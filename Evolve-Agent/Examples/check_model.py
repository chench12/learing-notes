import json
from pathlib import Path

from Evolve.llm import ChatModel


model = ChatModel(
    base_url="https://sub.callai.one/v1",
    model="gpt-6.1-sol",
    key_env="RELAY_API_KEY",
)

result = model.generate(
    messages=[
        {
            "role": "user",
            "content": "请仅回复 MODEL_CONNECTION_OK",
        }
    ],
    max_tokens=64,

)

project_root = Path(__file__).resolve().parents[1]
output_dir = project_root / "runs" / "connection_check"
output_dir.mkdir(parents=True, exist_ok=True)

(output_dir / "response.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print("模型：", result["model"])
print("回复：", result["text"])
print("结束原因：", result["finish_reason"])
print("Token 用量：", result["usage"])
print("耗时：", result["elapsed_seconds"])