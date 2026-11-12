"""单实验有界流水线：协调器唯一写库，线程仅执行外部请求或候选。

实验锁跨越整个运行；所有持久化都在拥有锁的事件循环线程完成。
取消只停止准入，已开始的线程必须排空，不能把外部副作用当作撤销。
"""

import asyncio
from time import perf_counter

from Evolve.budget import BudgetExceeded, check_budget, usage_snapshot
from Evolve.calls import prepare_call, finish_call, fail_call
from Evolve.contracts import Evaluation, ExecutionLimits
from Evolve.controller import confirm_generation, save_control
from Evolve.experiment import new_id
from Evolve.generation import (TERMINAL_FAILURES, prepare_generation, generation_state,
                               process_saved_response, finish_generation)
from Evolve.meta_prompts import maybe_evolve_template, template_credit
from Evolve.recovery import recover_experiment
from Evolve.routing import ModelPool, invoke
from Evolve.runner import run_candidate
from Evolve.session import now


class Pipeline:
    def __init__(self, session, model, control, stopped):
        self.session, self.model, self.control = session, model, control
        self.stopped = stopped
        config = session.config["pipeline"]
        self.planning = asyncio.Lock()
        self.models = asyncio.Semaphore(config["model_concurrency"])
        self.evaluations = asyncio.Semaphore(config["evaluation_concurrency"])
        self.halt_reason = None
        self.error_type = None
        self.active_models = self.active_evaluations = 0

    def halt(self, reason, exc=None):
        if self.halt_reason is None:
            self.halt_reason = reason
            self.error_type = type(exc).__name__ if exc else None

    def admission_closed(self):
        if self.stopped():
            self.halt("stop_requested")
        return self.halt_reason is not None

    async def evaluate(self, candidate, role, *, evaluation_id=None, generation_id=None):
        # 等待槽位时不写running；子代已由生成记录持有完整评测预留。
        async with self.evaluations:
            record = self.session.begin_evaluation(candidate, role, evaluation_id=evaluation_id,
                                                    reservation_generation_id=generation_id)
            if record["state"] == "completed":
                return record["evaluation_attempt_id"], Evaluation(**record["result"])
            if generation_id is not None:
                generation = self.session.storage.get("generation", generation_id)
                generation_state(self.session, generation, "evaluating", evaluation_attempt_id=record["evaluation_attempt_id"])
            self.active_evaluations += 1
            self.control["peak_evaluations"] = max(self.control["peak_evaluations"], self.active_evaluations)
            started = perf_counter()
            try:
                result = await asyncio.to_thread(run_candidate, self.session.task, candidate,
                    artifact_dir=self.session.directory / "evaluations" / record["evaluation_attempt_id"] / "artifacts",
                    limits=ExecutionLimits(**self.session.config["execution"]))
                if result.candidate_id != candidate.candidate_id or result.stage != "standard":
                    raise ValueError("评测结果的候选编号或阶段与本次计划不一致")
            except BaseException as exc:
                self.session.fail_evaluation(record, exc, perf_counter() - started)
                raise
            finally:
                self.active_evaluations -= 1
            return self.session.finish_evaluation(record, result, perf_counter() - started)

    async def request(self, generation):
        plan = generation["plan"]
        async with self.models:
            if self.admission_closed():
                generation_state(self.session, generation, "not_sent", reason="admission_closed")
                return None
            # 预算预留与状态提交之间没有await；其它协程无法同时通过旧余额。
            call = prepare_call(self.session, plan["model"], plan["messages"], purpose="program",
                                owner_id=plan["generation_attempt_id"], call_id=plan["call_id"])
            generation_state(self.session, generation, "requesting")
            self.active_models += 1
            self.control["peak_model_calls"] = max(self.control["peak_model_calls"], self.active_models)
            started = perf_counter()
            try:
                response = await asyncio.to_thread(invoke, self.model, plan["model"],
                    messages=plan["messages"], max_tokens=call["plan"]["max_tokens"])
                if not isinstance(response, dict):
                    raise TypeError("模型接口必须返回字典")
            except BaseException as exc:
                fail_call(self.session, call, exc, perf_counter() - started)
                generation_state(self.session, generation, call["state"], error_type=type(exc).__name__)
                raise
            finally:
                self.active_models -= 1
            finish_call(self.session, call, response, perf_counter() - started)
            generation["response"] = response
            generation_state(self.session, generation, "response_received")
            return response

    async def child(self, identifier):
        candidate, evaluation_id = process_saved_response(self.session, identifier, prepare_only=True)
        evaluation_id, result = await self.evaluate(candidate, "child", evaluation_id=evaluation_id, generation_id=identifier)
        return finish_generation(self.session, identifier, candidate, evaluation_id, result)

    async def generation(self):
        generation = None
        try:
            # 选择序号、父代复测、上下文与计划提交串行；远程请求/子代执行可重叠。
            async with self.planning:
                if self.admission_closed():
                    return
                check_budget(self.session)
                if usage_snapshot(self.session)["calls"] >= self.session.config["budget"]["max_calls"]:
                    raise BudgetExceeded("已达到模型调用预算")
                self.session.sync_archive()
                parent, selection = self.session.select_parent()
                pool = self.session.selection_snapshot()
                evaluation_id, result = await self.evaluate(parent, "parent")
                if result.status != "ok":
                    raise RuntimeError("父代本次评测不合法，停止流水线")
                if self.admission_closed():
                    return
                generation = prepare_generation(self.session, parent, selection, evaluation_id, result, pool, reserve_child=True)
            response = await self.request(generation)
            if response is not None:
                await self.child(generation["plan"]["generation_attempt_id"])
        except BudgetExceeded as exc:
            if generation is not None and "response" not in generation:
                call = self.session.storage.get("call", generation["plan"]["call_id"])
                if call is None:
                    generation_state(self.session, generation, "budget_stopped")
            # 已保存响应不标作终止：保留子代预留，日后可继续离线处理。
            self.halt("budget_exhausted", exc)
        except Exception as exc:
            current = self.session.storage.get("generation", generation["plan"]["generation_attempt_id"]) if generation else None
            if current and current["state"]["status"] in TERMINAL_FAILURES - {"api_error"}:
                return  # 明确的解析/骨架失败允许其他独立尝试继续。
            self.halt("pipeline_error", exc)


async def _run(session, model, *, steps, reviewed_fixtures, stop_requested, cancellation):
    if session.config["pipeline"]["mode"] != "async":
        raise ValueError("异步入口要求冻结的 pipeline.mode=async，请创建新实验")
    if steps is not None and (type(steps) is not int or steps <= 0):
        raise ValueError("steps 必须为正整数")
    if reviewed_fixtures and (model is None or isinstance(model, ModelPool)):
        raise ValueError("离线模式需要显式的手写响应夹具")
    if not reviewed_fixtures:
        if session.config["execution"]["backend"] != "docker":
            raise RuntimeError("真实自动流水线要求Docker，不回退本地执行")
        from Evolve.docker_execution import inspect_runtime, inspect_image
        await asyncio.to_thread(inspect_runtime)
        await asyncio.to_thread(inspect_image, session.config["execution"]["docker_image"])
    model = model or ModelPool()
    with session.storage.operation():
        recover_experiment(session)
        if any(call["state"] in {"requesting", "uncertain"} for _, call in session.storage.all("call")):
            raise RuntimeError("存在结果未知的模型调用，停止续跑，不自动重发")
        record = {"control_id": new_id("control"), "state": "running", "started_at_utc": now(),
                  "steps_limit": steps, "new_generations": 0, "controller_mode": "async-v1",
                  "model_mode": "handwritten-fixtures" if reviewed_fixtures else "configured-remote",
                  "pipeline": dict(session.config["pipeline"]), "peak_inflight": 0,
                  "peak_model_calls": 0, "peak_evaluations": 0}
        save_control(session, record)
        started, initial = perf_counter(), session.generation_count()
        stopped = lambda: (cancellation.is_set() or (session.directory / "STOP").exists()
                           or (stop_requested is not None and stop_requested()))
        pipeline = Pipeline(session, model, record, stopped)
        pending = set()
        target = min(steps if steps is not None else session.config["search"]["max_generations"],
                     session.config["search"]["max_generations"] - initial)
        admitted = 0
        reason = "step_limit"
        barrier = False
        try:
            # 恢复先排空旧响应；没有任何客户端请求参与此过程。
            for identifier, generation in session.storage.all("generation"):
                status = generation["state"]["status"]
                if status not in TERMINAL_FAILURES | {"completed"}:
                    if "response" not in generation:
                        raise RuntimeError("遗留生成没有已保存响应，不自动重发")
                    await pipeline.child(identifier)
                confirm_generation(session, session.storage.get("generation", identifier))
            while True:
                closed = pipeline.admission_closed()
                if not pending and not closed:
                    # 提示词与重复确认在排空点串行，避免在途条件改变其信用或抢占预算。
                    for _, generation in session.storage.all("generation"):
                        confirm_generation(session, generation)
                    if admitted < target:
                        maybe_evolve_template(session, model)
                    barrier = False
                while not closed and not barrier and admitted < target and len(pending) < session.config["pipeline"]["max_inflight"]:
                    pending.add(asyncio.create_task(pipeline.generation()))
                    admitted += 1
                    record["peak_inflight"] = max(record["peak_inflight"], len(pending))
                if not pending:
                    reason = pipeline.halt_reason or ("generation_budget" if session.generation_count() >= session.config["search"]["max_generations"] else "step_limit")
                    break
                # 短周期检查STOP；不取消已经发出的请求或执行中的评测。
                done, pending = await asyncio.wait(pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                if done:
                    record["new_generations"] = session.generation_count() - initial
                    save_control(session, record)
                    complete = sum(item["state"]["status"] == "completed" for _, item in session.storage.all("generation"))
                    barrier = session.config["confirmation"]["enabled"] or (session.config["meta"]["enabled"] and complete >= session.config["meta"]["interval"])
            # STOP/预算关闭准入后也记录已经完成的子代，复核可留给下次启动。
            if not pipeline.halt_reason:
                for _, generation in session.storage.all("generation"):
                    confirm_generation(session, generation)
        except BudgetExceeded:
            reason = "budget_exhausted"
        except BaseException as exc:
            pipeline.halt("pipeline_error", exc)
            # 即使协调器失败也必须等外部副作用结束，再释放实验锁。
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            record.update(state="interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else "failed",
                          error_type=type(exc).__name__, elapsed_seconds=perf_counter() - started,
                          finished_at_utc=now(), new_generations=session.generation_count() - initial,
                          usage=usage_snapshot(session))
            save_control(session, record)
            raise
        session.sync_archive()
        record.update(state="failed" if reason == "pipeline_error" else "stopped", stop_reason=reason,
                      elapsed_seconds=perf_counter() - started, finished_at_utc=now(),
                      new_generations=session.generation_count() - initial,
                      usage=usage_snapshot(session), template_credit=template_credit(session))
        if pipeline.error_type:
            record["error_type"] = pipeline.error_type
        save_control(session, record)
        return record


async def run_search_async(session, model=None, *, steps=None, reviewed_fixtures=False, stop_requested=None):
    """取消=关闭准入并等待在途操作落盘；完成排空后才向调用者传播取消。"""
    if getattr(session, "_async_controller_active", False):
        raise RuntimeError("同一实验会话已有异步控制器运行")
    session._async_controller_active = True
    cancellation = asyncio.Event()
    task = asyncio.create_task(_run(session, model, steps=steps, reviewed_fixtures=reviewed_fixtures,
                                    stop_requested=stop_requested, cancellation=cancellation))
    try:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancellation.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()  # 取回异常，控制记录保留实际错误；外层仍传播取消。
            raise
    finally:
        session._async_controller_active = False
