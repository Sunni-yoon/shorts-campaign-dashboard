"""포맷 분류 검증: 사람 라벨링용 표본 내보내기 + 정확도·오분류 분석.

※ 2026-11 결정: 단일 포맷 정확도 검증은 생략 (CLAUDE.md). 다중 라벨 도입 시 재사용할 수 있도록 남겨 둠.

사용법:
  venv\\Scripts\\python.exe format_eval.py export      # data/labels/label_sample_50.csv 생성 (LLM 결과 숨김)
  (CSV의 label 열에 정답 포맷 입력: 챌린지 / 레시피·조리 / 먹방·ASMR·맛평가 / 코미디·상황극 / 기타)
  venv\\Scripts\\python.exe format_eval.py evaluate    # 정확도, 포맷별 정밀도·재현율, 혼동표, 오분류 목록

파일은 data/labels/에 저장된다 (data/는 git 제외 — 수집 데이터가 들어 있음).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

import config

LABEL_DIR = config.DB_PATH.parent / "labels"
SAMPLE_PATH = LABEL_DIR / f"label_sample_{config.FORMAT_ACCURACY_SAMPLE}.csv"
ERRORS_PATH = LABEL_DIR / "errors.csv"
GUIDE_PATH = LABEL_DIR / "라벨링_안내.txt"
SEED = 20261126

GUIDE = f"""라벨링 안내 — label 열에 아래 포맷 중 하나를 정확히 입력하세요.
{chr(10).join('- ' + f for f in config.FORMATS)}

겹치면 위의 규칙이 우선합니다 (CLAUDE.md와 같음).
1. 도전 구조(challenge, vs, 상금, 몇 입에)가 제목에 있으면 → 챌린지
2. 만드는 과정이 중심이면 → 레시피·조리
3. 먹거나 맛을 평가하는 영상이면 → 먹방·ASMR·맛평가
4. 어디에도 안 맞으면 → 기타
ASMR은 다른 포맷 신호가 없을 때만 먹방·ASMR·맛평가입니다.

LLM과 같은 조건으로 비교하려면 제목·설명·해시태그만 보고 판단하세요.
(URL로 영상을 직접 보고 판단해도 되지만, 그 경우 '영상 기준 정답'과 비교하는 것이 됩니다.)
"""


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True)


def export(n: int = config.FORMAT_ACCURACY_SAMPLE, overwrite: bool = False) -> Path:
    """최신 실행(현재 수집 기준)의 분류된 쇼츠 중 n개를 고정 시드로 무작위 추출. LLM 결과 열은 넣지 않음."""
    if SAMPLE_PATH.exists() and not overwrite:
        raise FileExistsError(f"{SAMPLE_PATH}가 이미 있습니다 (라벨 보호). 새로 만들려면 --overwrite")
    with _connect() as conn:
        df = pd.read_sql_query(
            """
            SELECT v.video_id, GROUP_CONCAT(DISTINCT rkv.keyword) AS keywords,
                   v.title, v.description, v.hashtags
            FROM run_keyword_videos rkv
            JOIN videos v ON v.video_id = rkv.video_id
            JOIN video_formats f ON f.video_id = v.video_id AND f.prompt_version = :pv
            WHERE rkv.run_id = (SELECT MAX(run_id) FROM runs
                                WHERE criteria = :c AND mode = 'search' AND status = 'ok')
              AND v.is_short = 1
            GROUP BY v.video_id ORDER BY v.video_id
            """,
            conn, params={"pv": config.FORMAT_PROMPT_VERSION, "c": config.CRITERIA},
        )
    if len(df) < n:
        raise ValueError(f"분류된 영상이 {len(df)}개뿐이라 {n}개를 뽑을 수 없습니다. 먼저 classify_formats.py를 실행하세요.")
    s = df.sample(n=n, random_state=SEED).reset_index(drop=True)
    out = pd.DataFrame({
        "no": range(1, n + 1),
        "video_id": s["video_id"],
        "url": "https://www.youtube.com/watch?v=" + s["video_id"],
        "keywords": s["keywords"],
        "title": s["title"],
        "description": s["description"].fillna("").str.slice(0, 500),
        "hashtags": s["hashtags"].map(lambda h: " ".join(f"#{t}" for t in json.loads(h or "[]"))),
        "label": "",
        "memo": "",
    })
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(SAMPLE_PATH, index=False, encoding="utf-8-sig")   # 엑셀에서 한글이 깨지지 않게
    GUIDE_PATH.write_text(GUIDE, encoding="utf-8")
    return SAMPLE_PATH


def evaluate(path: Path = SAMPLE_PATH) -> dict:
    labels = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    labels["label"] = labels["label"].str.strip()
    filled = labels[labels["label"] != ""]
    invalid = filled[~filled["label"].isin(config.FORMATS)]
    valid = filled[filled["label"].isin(config.FORMATS)]

    with _connect() as conn:
        llm = pd.read_sql_query(
            "SELECT video_id, format AS llm, confidence, reason FROM video_formats WHERE prompt_version = ?",
            conn, params=[config.FORMAT_PROMPT_VERSION],
        )
    m = valid.merge(llm, on="video_id", how="left")
    missing = m[m["llm"].isna()]
    m = m.dropna(subset=["llm"])
    m["correct"] = m["label"] == m["llm"]

    per_format = []
    for f in config.FORMATS:
        tp = int(((m["label"] == f) & (m["llm"] == f)).sum())
        pred, actual = int((m["llm"] == f).sum()), int((m["label"] == f).sum())
        per_format.append({"format": f, "정답 수": actual, "LLM 예측 수": pred, "맞힘": tp,
                           "정밀도": tp / pred if pred else None, "재현율": tp / actual if actual else None})
    confusion = pd.crosstab(m["label"].rename("정답"), m["llm"].rename("LLM")).reindex(
        index=config.FORMATS, columns=config.FORMATS, fill_value=0)
    by_conf = m.groupby("confidence")["correct"].agg(["count", "mean"]).reindex(config.FORMAT_CONFIDENCES)

    errors = m[~m["correct"]][["no", "video_id", "url", "title", "label", "llm", "confidence", "reason", "memo"]]
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    errors.rename(columns={"label": "정답", "llm": "LLM"}).to_csv(ERRORS_PATH, index=False, encoding="utf-8-sig")
    pairs = (errors.groupby(["label", "llm"]).size().sort_values(ascending=False)
             .rename("건수").reset_index().rename(columns={"label": "정답", "llm": "LLM"}))
    return {"n": len(m), "accuracy": float(m["correct"].mean()) if len(m) else None,
            "per_format": pd.DataFrame(per_format), "confusion": confusion, "by_confidence": by_conf,
            "error_pairs": pairs, "unlabeled": int((labels["label"] == "").sum()),
            "invalid": invalid[["no", "label"]], "missing_llm": int(len(missing))}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="포맷 분류 검증")
    parser.add_argument("mode", choices=["export", "evaluate"])
    parser.add_argument("--overwrite", action="store_true", help="export: 기존 라벨 파일 덮어쓰기")
    parser.add_argument("--file", type=Path, default=SAMPLE_PATH)
    args = parser.parse_args()

    if args.mode == "export":
        try:
            path = export(overwrite=args.overwrite)
        except (FileExistsError, ValueError) as e:
            print(e)
            return 1
        print(f"저장: {path}\n안내: {GUIDE_PATH}\nlabel 열에 정답 포맷을 입력한 뒤 `format_eval.py evaluate`를 실행하세요.")
        return 0

    r = evaluate(args.file)
    pd.set_option("display.width", 200)
    print(f"[포맷 분류 검증] 라벨 {r['n']}개 · 미입력 {r['unlabeled']}개 · 잘못된 라벨 {len(r['invalid'])}개"
          f" · LLM 결과 없음 {r['missing_llm']}개")
    if len(r["invalid"]):
        print("  잘못된 라벨(목록에 없는 값):", r["invalid"].to_dict("records"))
    if r["accuracy"] is None:
        print("계산할 라벨이 없습니다.")
        return 1
    print(f"\n정확도: {r['accuracy']:.1%} ({int(round(r['accuracy'] * r['n']))}/{r['n']})")
    print("\n포맷별", r["per_format"].round(2).to_string(index=False), sep="\n")
    print("\n혼동표 (행=정답, 열=LLM)", r["confusion"].to_string(), sep="\n")
    print("\n확신도별 정확도", r["by_confidence"].rename(columns={"count": "개수", "mean": "정확도"}).round(2).to_string(),
          sep="\n")
    if len(r["error_pairs"]):
        print("\n자주 틀린 조합", r["error_pairs"].to_string(index=False), sep="\n")
    print(f"\n오분류 목록: {ERRORS_PATH}")
    print(f"대시보드 반영: config.py의 FORMAT_ACCURACY = {r['accuracy']:.2f} 로 바꾸고 커밋하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
