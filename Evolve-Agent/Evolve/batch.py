"""独立实验的有界异步调度；每个实验内部仍串行，保持状态和评测预算一致。"""

import asyncio

from Evolve.controller import run_search


async def run_batch(sessions, *, concurrency=1, steps=None, model_factory=None, reviewed_fixtures=False):
    if type(concurrency) is not int or concurrency <= 0:
        raise ValueError("concurrency 必须为正整数")
    if len({str(session.directory) for session in sessions}) != len(sessions):
        raise ValueError("同一实验不能重复入队")
    if reviewed_fixtures and model_factory is None:
        raise ValueError("离线批处理需要显式手写夹具")
    semaphore = asyncio.Semaphore(concurrency)

    async def run(session):
        async with semaphore:
            try:
                result = await asyncio.to_thread(run_search, session,
                    model_factory(session) if model_factory else None,
                    steps=steps, reviewed_fixtures=reviewed_fixtures,
                    stop_requested=lambda: (session.directory / "STOP").exists())
                return {"experiment_id": session.experiment_id, "status": "stopped", "control": result}
            except Exception as exc:
                return {"experiment_id": session.experiment_id, "status": "failed", "error_type": type(exc).__name__}

    # 只有指定数量工作线程运行；不在同一实验上并发更新档案。
    return await asyncio.gather(*(run(session) for session in sessions))
