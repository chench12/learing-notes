import os
from time import perf_counter

from openai import OpenAI


class ChatModel:
    def __init__(self, *, base_url, model, key_env):
        api_key = os.environ.get(key_env)

        if not api_key:
            raise ValueError(f"缺少环境变量：{key_env}")

        self.model = model
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=120.0,
            max_retries=0,
        )

    def generate(
        self,
        messages,
        *,
        max_tokens=2048,
        extra_body=None,
    ):
        started = perf_counter()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            stream=False,
            extra_body=extra_body,
        )

        choice = response.choices[0]

        return {
            "text": choice.message.content,
            "model": response.model,
            "response_id": response.id,
            "finish_reason": choice.finish_reason,
            "usage": (
                response.usage.model_dump()
                if response.usage is not None
                else None
            ),
            "elapsed_seconds": perf_counter() - started,
        }