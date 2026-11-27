"""댓글 반응 분류 (OpenAI). 댓글 하나씩 독립적으로 판단 — 다른 댓글·영상 정보는 보내지 않음.

- 감성: 긍정 / 부정 / 중립 (하나만, 판단 불가는 중립)
- 구매의향: 있음 / 없음 (감성과 별도 판단)
- 저장: comment_reactions (모델·프롬프트 버전·분류 시각·토큰 수)
- 같은 프롬프트 버전으로 이미 분류한 댓글은 다시 분류하지 않음
- 추론 강도 config.COMMENT_REASONING_EFFORT, 실패한 호출의 토큰도 비용에 포함
- 호출은 독립적이지만 동시에 config.COMMENT_WORKERS개씩 보내 시간을 줄임

사용법: venv\\Scripts\\python.exe classify_comments.py
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import classify_formats
import config
import db
import keys

# 감성 정의 — 프롬프트와 대시보드 설명이 같은 문구를 씀 (바꾸면 프롬프트가 바뀌므로 COMMENT_PROMPT_VERSION을 올릴 것)
SENTIMENT_DEFINITIONS = {
    "긍정": "칭찬, 좋아함, 맛있어 보임, 감탄, 응원",
    "부정": "비판, 싫어함, 역겨움, 불만, 조롱",
    "중립": "질문, 정보, 사실 진술, 섞인 감정, 의미를 판단할 수 없음",
}
_SENTIMENT_LINES = "\n".join(f"- {k}: {v}" for k, v in SENTIMENT_DEFINITIONS.items())

SYSTEM_PROMPT = f"""너는 YouTube 쇼츠(라면·매운 음식 영상)에 달린 댓글 하나의 반응을 분류한다. 댓글 하나만 보고 판단한다.

sentiment (하나만): {" / ".join(config.SENTIMENTS)}
{_SENTIMENT_LINES}
purchase_intent: {" / ".join(config.PURCHASE_INTENTS)} — sentiment와 별도로 판단한다.
- 있음: 제품을 사거나 먹어 보려는 의사·관심 (예: "where can I buy", "I need to try this", "going to get some",
  "어디서 사요", "먹어 보고 싶다", "사러 간다")
- 없음: 그 외 모두, 또는 애매한 경우

영어 외 언어도 그대로 이해해 판단한다. 이모지만 있는 댓글도 이모지의 뜻으로 판단한다 (예: 😍🔥 긍정, 🤢 부정).
판단할 수 없으면 sentiment는 중립, purchase_intent는 없음."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "string", "enum": config.SENTIMENTS},
        "purchase_intent": {"type": "string", "enum": config.PURCHASE_INTENTS},
    },
    "required": ["sentiment", "purchase_intent"],
    "additionalProperties": False,
}


def parse_response(text: str) -> dict:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as e:
        raise classify_formats.ParseError(f"JSON이 아님: {e}") from None
    if data.get("sentiment") not in config.SENTIMENTS or data.get("purchase_intent") not in config.PURCHASE_INTENTS:
        raise classify_formats.ParseError(f"목록에 없는 값: {data!r}")
    return {"sentiment": data["sentiment"], "purchase_intent": data["purchase_intent"]}


class OpenAICommentClassifier:
    def __init__(self, api_key: str, model: str = config.COMMENT_MODEL):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, max_retries=3, timeout=60)
        self.model = model

    def classify(self, text: str) -> tuple[dict, dict]:
        resp = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=f"댓글: {text[: config.COMMENT_TEXT_CHARS]}",
            reasoning={"effort": config.COMMENT_REASONING_EFFORT},
            max_output_tokens=config.COMMENT_MAX_OUTPUT_TOKENS,
            text={"format": {"type": "json_schema", "name": "comment_reaction",
                             "schema": RESPONSE_SCHEMA, "strict": True}},
        )
        u = resp.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        usage = {"input": u.input_tokens, "cached_input": cached, "output": u.output_tokens,
                 "model": getattr(resp, "model", self.model)}
        try:
            if getattr(resp, "status", "completed") != "completed":
                raise classify_formats.ParseError(f"응답 미완료: {resp.status}")
            return parse_response(resp.output_text), usage
        except classify_formats.ParseError as e:
            e.usage = usage                    # 실패한 호출도 과금되므로 비용 집계에 포함
            raise


def pending_comments(conn, prompt_version: str | None = None) -> list[tuple[str, str]]:
    """최신 실행(현재 수집 기준) 쇼츠의 댓글 중 이 프롬프트 버전으로 아직 분류하지 않은 것 (comment_key, text)."""
    prompt_version = prompt_version or config.COMMENT_PROMPT_VERSION
    return conn.execute(
        """
        SELECT c.comment_key, c.text FROM video_comments c
        WHERE c.video_id IN (
            SELECT rkv.video_id FROM run_keyword_videos rkv JOIN videos v ON v.video_id = rkv.video_id
            WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs
                                WHERE criteria = ? AND mode = 'search' AND status = 'ok')
              AND v.is_short = 1)
          AND NOT EXISTS (SELECT 1 FROM comment_reactions r
                          WHERE r.comment_key = c.comment_key AND r.prompt_version = ?)
        ORDER BY c.video_id, c.rank
        """,
        (config.CRITERIA, prompt_version),
    ).fetchall()


def classify_pending(conn, api_key: str, classifier=None, progress=None, workers: int | None = None,
                     limit: int | None = None) -> dict:
    """미분류 댓글을 하나씩(동시 요청) 분류해 저장. 예외를 던지지 않고 요약을 돌려줌."""
    rows = pending_comments(conn)
    if limit:
        rows = rows[:limit]
    result = {"pending": len(rows), "classified": 0, "failed": 0, "errors": [], "stopped": False,
              "usage": {"input": 0, "cached_input": 0, "output": 0}, "model": None, "cost_usd": 0.0}
    if not rows:
        return result
    try:
        classifier = classifier or OpenAICommentClassifier(api_key)
    except Exception as e:
        result.update(failed=len(rows), stopped=True, errors=[keys.mask(f"{type(e).__name__}: {e}", api_key)])
        return result

    def work(row):
        key, text = row
        if not (text or "").strip():
            return key, ({"sentiment": "중립", "purchase_intent": "없음"},
                         {"input": 0, "cached_input": 0, "output": 0, "model": "rule:empty-text"}), None
        try:
            return key, classifier.classify(text), None
        except Exception as e:
            return key, (None, getattr(e, "usage", None)), keys.mask(f"{type(e).__name__}: {e}", api_key)

    workers = workers or config.COMMENT_WORKERS
    chunk = max(workers * 10, 1)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(rows), chunk):
            batch = rows[start:start + chunk]
            for key, out, err in pool.map(work, batch):
                done += 1
                if out and out[1]:                    # 성공·실패 모두 실제 사용 토큰 집계
                    for k in ("input", "cached_input", "output"):
                        result["usage"][k] += out[1].get(k, 0) or 0
                if err:
                    result["failed"] += 1
                    if len(result["errors"]) < 5:
                        result["errors"].append(err)
                    if any(s in err for s in classify_formats.STOP_ERRORS):
                        result["stopped"] = True
                    continue
                parsed, usage = out
                if usage.get("input"):                   # 실제 API 호출만 모델 이름 집계
                    result["model"] = usage.get("model") or config.COMMENT_MODEL
                conn.execute(
                    """INSERT OR REPLACE INTO comment_reactions
                       (comment_key, prompt_version, sentiment, purchase_intent, model, classified_at,
                        input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, config.COMMENT_PROMPT_VERSION, parsed["sentiment"], parsed["purchase_intent"],
                     usage.get("model") or config.COMMENT_MODEL,
                     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     usage.get("input"), usage.get("output")),
                )
                result["classified"] += 1
            conn.commit()
            if progress:
                progress(f"댓글 분류 중 {done}/{len(rows)}")
            if result["stopped"]:                     # 키·크레딧 문제 → 남은 댓글은 보내지 않음
                result["failed"] = len(rows) - result["classified"]
                break
    result["cost_usd"] = classify_formats.cost_usd(result["usage"])
    return result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    api_key = keys.get_api_key(keys.OPENAI_KEY_NAME)
    if not api_key:
        print(f"{keys.OPENAI_KEY_NAME}가 .env(또는 환경변수)에 없습니다.")
        return 1
    conn = db.connect(config.DB_PATH)
    try:
        r = classify_pending(conn, api_key, progress=print)
        dist = conn.execute(
            "SELECT sentiment, purchase_intent, COUNT(*) FROM comment_reactions WHERE prompt_version = ? "
            "GROUP BY 1, 2 ORDER BY 1, 2", (config.COMMENT_PROMPT_VERSION,)).fetchall()
    finally:
        conn.close()
    print(f"\n[댓글 분류] 대상 {r['pending']} · 분류 {r['classified']} · 실패 {r['failed']}"
          + (" (중단)" if r["stopped"] else ""))
    for err in r["errors"]:
        print(f"  ! {classify_formats.explain_error(err)}")
    u = r["usage"]
    print(f"모델: {r['model'] or config.COMMENT_MODEL} (reasoning {config.COMMENT_REASONING_EFFORT}) · "
          f"프롬프트 {config.COMMENT_PROMPT_VERSION} · "
          f"토큰 입력 {u['input']:,}(캐시 {u['cached_input']:,}) / 출력 {u['output']:,} · 비용 ${r['cost_usd']:.4f}")
    print("감성 × 구매의향 (댓글 수):", dist)
    return 0 if not r["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
