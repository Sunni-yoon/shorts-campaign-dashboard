"""쇼츠 콘텐츠 포맷 분류 (OpenAI). 대시보드와 분리된 모듈 — CLI·대시보드 공용.

- 입력: 제목, 설명(앞 config.FORMAT_DESCRIPTION_CHARS자), 해시태그. 영상 내용은 보지 않음
- 출력: JSON {format, confidence, reason} — JSON 스키마(enum)로 포맷을 목록 안으로 강제
- 저장: video_formats 테이블 (모델·프롬프트 버전·분류 시각·토큰 수 함께)
- 같은 프롬프트 버전으로 이미 분류한 영상은 다시 분류하지 않음
- 키는 keys.get_api_key("OPENAI_API_KEY")로 읽고 출력하지 않음

사용법: venv\\Scripts\\python.exe classify_formats.py   # 최신 v2 실행의 미분류 쇼츠만 분류
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone

import config
import db
import keys

SYSTEM_PROMPT = f"""너는 YouTube 쇼츠의 콘텐츠 포맷을 분류한다. 영상은 볼 수 없고 제목·설명·해시태그만 주어진다.
포맷은 다음 중 하나만 고른다: {", ".join(config.FORMATS)}

겹치면 위에 있는 규칙이 우선한다.
1. 도전 구조(challenge, vs, 상금/prize, 몇 입에/how many bites, 시간 제한 등)가 제목에 있으면 → 챌린지
2. 만드는 과정(recipe, how to make, cooking, making)이 중심이면 → 레시피·조리
3. 먹거나 맛을 평가하는 영상(mukbang, eating, taste test, review, trying for the first time)이면 → 먹방·ASMR·맛평가
4. 어디에도 안 맞으면(모음·랭킹 편집, 정보·브랜드 스토리, 판단 정보 부족 등) → 기타
코미디·상황극은 별도 포맷으로 두지 않는다. 그런 영상도 위 1~3의 신호를 따르고, 없으면 기타로 분류한다.

ASMR은 포맷이 아니라 연출 방식이다. 'ASMR'만 보고 판단하지 말고, 다른 포맷 신호가 없을 때만 먹방·ASMR·맛평가로 분류한다.
확신도: 신호가 분명하면 높음, 신호가 약하거나 규칙끼리 경합하면 중간, 추측에 가까우면 낮음.
reason: 판단에 쓴 단어나 근거를 한국어 한 줄(60자 이내)로 쓴다."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": config.FORMATS},
        "confidence": {"type": "string", "enum": config.FORMAT_CONFIDENCES},
        "reason": {"type": "string"},
    },
    "required": ["format", "confidence", "reason"],
    "additionalProperties": False,
}

STOP_ERRORS = ("AuthenticationError", "PermissionDeniedError", "insufficient_quota", "NotFoundError")


class ParseError(Exception):
    """모델 응답이 스키마와 맞지 않음."""


def build_input(title: str | None, description: str | None, hashtags: list[str]) -> str:
    desc = (description or "")[: config.FORMAT_DESCRIPTION_CHARS]
    tags = " ".join(f"#{t}" for t in hashtags) or "(없음)"
    return f"제목: {title or ''}\n설명: {desc or '(없음)'}\n해시태그: {tags}"


def parse_response(text: str) -> dict:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as e:
        raise ParseError(f"JSON이 아님: {e}") from None
    if data.get("format") not in config.FORMATS:
        raise ParseError(f"목록에 없는 포맷: {data.get('format')!r}")
    if data.get("confidence") not in config.FORMAT_CONFIDENCES:
        raise ParseError(f"목록에 없는 확신도: {data.get('confidence')!r}")
    return {"format": data["format"], "confidence": data["confidence"],
            "reason": str(data.get("reason", ""))[:200]}


class OpenAIClassifier:
    """Responses API + JSON 스키마. 추론 강도 none으로 출력 토큰(비용) 최소화."""

    def __init__(self, api_key: str, model: str = config.FORMAT_MODEL):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, max_retries=2, timeout=60)
        self.model = model

    def classify(self, text: str) -> tuple[dict, dict]:
        resp = self.client.responses.create(
            model=self.model,
            instructions=SYSTEM_PROMPT,
            input=text,
            reasoning={"effort": "none"},
            max_output_tokens=300,
            text={"format": {"type": "json_schema", "name": "shorts_format",
                             "schema": RESPONSE_SCHEMA, "strict": True}},
        )
        u = resp.usage
        cached = getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
        usage = {"input": u.input_tokens, "cached_input": cached, "output": u.output_tokens,
                 "model": getattr(resp, "model", self.model)}
        try:
            return parse_response(resp.output_text), usage
        except ParseError as e:
            e.usage = usage                    # 실패한 호출도 과금되므로 비용 집계에 포함
            raise


def cost_usd(usage: dict) -> float:
    p = config.FORMAT_PRICE_PER_1M
    fresh = usage.get("input", 0) - usage.get("cached_input", 0)
    return (fresh * p["input"] + usage.get("cached_input", 0) * p["cached_input"]
            + usage.get("output", 0) * p["output"]) / 1_000_000


def pending_videos(conn, criteria: str | None = None, prompt_version: str | None = None) -> list[tuple]:
    """최신 성공 search 실행의 쇼츠 중 이 프롬프트 버전으로 아직 분류하지 않은 영상."""
    criteria = criteria or config.CRITERIA
    prompt_version = prompt_version or config.FORMAT_PROMPT_VERSION
    return conn.execute(
        """
        SELECT DISTINCT v.video_id, v.title, v.description, v.hashtags
        FROM run_keyword_videos rkv
        JOIN videos v ON v.video_id = rkv.video_id
        WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs
                            WHERE criteria = ? AND mode = 'search' AND status = 'ok')
          AND v.is_short = 1
          AND NOT EXISTS (SELECT 1 FROM video_formats f
                          WHERE f.video_id = v.video_id AND f.prompt_version = ?)
        ORDER BY v.video_id
        """,
        (criteria, prompt_version),
    ).fetchall()


def explain_error(error: str | None) -> str:
    if not error:
        return ""
    if "insufficient_quota" in error:
        hint = "OpenAI 계정의 크레딧(결제 한도)이 부족합니다."
    elif "AuthenticationError" in error or "invalid_api_key" in error or "Incorrect API key" in error:
        hint = "OpenAI API 키가 올바르지 않습니다. Secrets(배포) 또는 .env(로컬)의 OPENAI_API_KEY를 확인하세요."
    elif "RateLimitError" in error:
        hint = "OpenAI 요청 한도에 걸렸습니다. 잠시 후 다시 시도됩니다."
    elif "NotFoundError" in error or "model_not_found" in error:
        hint = f"모델 {config.FORMAT_MODEL}을 사용할 수 없습니다. config.FORMAT_MODEL을 확인하세요."
    elif "APIConnectionError" in error or "Timeout" in error:
        hint = "OpenAI 서버에 연결하지 못했습니다(네트워크 오류)."
    else:
        hint = "LLM 분류 중 오류가 발생했습니다."
    return f"{hint} (원문: {error})"


def classify_pending(conn, api_key: str, classifier=None, progress=None, limit: int | None = None) -> dict:
    """미분류 영상을 분류해 저장. 예외를 던지지 않고 결과 요약을 돌려줌 (오류 메시지는 키가 가려짐)."""
    rows = pending_videos(conn)
    if limit:
        rows = rows[:limit]
    result = {"pending": len(rows), "classified": 0, "failed": 0, "errors": [], "stopped": False,
              "usage": {"input": 0, "cached_input": 0, "output": 0}, "model": None, "cost_usd": 0.0}
    if not rows:
        return result
    try:
        classifier = classifier or OpenAIClassifier(api_key)
    except Exception as e:
        result.update(failed=len(rows), stopped=True,
                      errors=[keys.mask(f"{type(e).__name__}: {e}", api_key)])
        return result

    for i, (vid, title, desc, tags_json) in enumerate(rows, 1):
        if progress and (i == 1 or i % 20 == 0 or i == len(rows)):
            progress(f"포맷 분류 중 {i}/{len(rows)}")
        try:
            tags = json.loads(tags_json) if tags_json else []
            parsed, usage = classifier.classify(build_input(title, desc, tags))
        except Exception as e:
            for k in ("input", "cached_input", "output"):
                result["usage"][k] += (getattr(e, "usage", None) or {}).get(k, 0) or 0
            msg = keys.mask(f"{type(e).__name__}: {e}", api_key)
            result["failed"] += 1
            if len(result["errors"]) < 5:
                result["errors"].append(f"{vid}: {msg}")
            if any(s in msg for s in STOP_ERRORS):   # 키·크레딧·모델 문제는 계속해도 실패 → 중단
                result["stopped"] = True
                result["failed"] = len(rows) - result["classified"]
                break
            continue
        for k in ("input", "cached_input", "output"):
            result["usage"][k] += usage.get(k, 0) or 0
        result["model"] = usage.get("model") or config.FORMAT_MODEL
        conn.execute(
            """INSERT OR REPLACE INTO video_formats
               (video_id, prompt_version, format, confidence, reason, model, classified_at,
                input_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vid, config.FORMAT_PROMPT_VERSION, parsed["format"], parsed["confidence"], parsed["reason"],
             result["model"], datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             usage.get("input"), usage.get("output")),
        )
        result["classified"] += 1
        if result["classified"] % 20 == 0:
            conn.commit()
    conn.commit()
    result["cost_usd"] = cost_usd(result["usage"])
    return result


def format_distribution(conn, prompt_version: str | None = None) -> list[tuple]:
    """최신 v2 실행 쇼츠의 포맷 분포 [(포맷, 개수)] — 미분류 포함."""
    prompt_version = prompt_version or config.FORMAT_PROMPT_VERSION
    return conn.execute(
        """
        SELECT COALESCE(f.format, '미분류'), COUNT(DISTINCT v.video_id)
        FROM run_keyword_videos rkv
        JOIN videos v ON v.video_id = rkv.video_id
        LEFT JOIN video_formats f ON f.video_id = v.video_id AND f.prompt_version = ?
        WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs
                            WHERE criteria = ? AND mode = 'search' AND status = 'ok')
          AND v.is_short = 1
        GROUP BY 1 ORDER BY 2 DESC
        """,
        (prompt_version, config.CRITERIA),
    ).fetchall()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    api_key = keys.get_api_key(keys.OPENAI_KEY_NAME)
    if not api_key:
        print(f"{keys.OPENAI_KEY_NAME}가 .env(또는 환경변수)에 없습니다.")
        return 1
    conn = db.connect(config.DB_PATH)
    try:
        r = classify_pending(conn, api_key, progress=print)
        dist = format_distribution(conn)
    except sqlite3.Error as e:
        print(f"DB 오류: {e}")
        return 1
    finally:
        conn.close()

    print(f"\n[포맷 분류] 대상 {r['pending']} · 분류 {r['classified']} · 실패 {r['failed']}"
          + (" (중단)" if r["stopped"] else ""))
    for err in r["errors"]:
        print(f"  ! {explain_error(err)}")
    u = r["usage"]
    print(f"모델: {r['model'] or config.FORMAT_MODEL} · 프롬프트 {config.FORMAT_PROMPT_VERSION} · "
          f"토큰 입력 {u['input']:,}(캐시 {u['cached_input']:,}) / 출력 {u['output']:,} · "
          f"비용 ${r['cost_usd']:.4f}")
    total = sum(n for f, n in dist if f != "미분류")
    print("\n포맷 분포 (최신 v2 실행 쇼츠, 영상 중복 제거)")
    for fmt, n in dist:
        share = f"{n / total:.1%}" if total and fmt != "미분류" else "-"
        print(f"  {fmt:<12} {n:>4}개  {share}")
    other = dict(dist).get("기타", 0)
    if total:
        flag = " → 15% 초과: 포맷 목록 재검토 필요" if other / total > config.FORMAT_OTHER_REVIEW_SHARE else ""
        print(f"기타 비율: {other / total:.1%} ({other}/{total}){flag}")
    return 0 if not r["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
