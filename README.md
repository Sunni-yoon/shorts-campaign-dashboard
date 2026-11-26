# 숏폼 캠페인 성과 리포트 대시보드

키워드(=캠페인 주제)별 YouTube 쇼츠 성과를 수집해 지표로 만들고, 브랜드가 의사결정에 쓸 수 있는 대시보드로 보여 주는 프로젝트입니다.

**대시보드:** https://shorts-campaign-dashboard-fiegkrrdeazp5sdzj3gbxm.streamlit.app/

대시보드는 결정 질문 순서로 구성되어 있습니다.

1. **우리 캠페인 기준선은?** 조회수·조회수 속도·참여율·구독자 대비 조회수의 중앙값
2. **어떤 주제가 잘 퍼지나?** 키워드별 평균·중앙값 요약표
3. **어떤 크리에이터와 일할까?** 채널 단위 집계, 구독자 수 범위 필터
4. **무엇을 참고해 만들까?** 지표별 상위 영상과 YouTube 링크
5. **세부 분포** 키워드별 참여율 분포, 조회수 vs 참여율

각 섹션 제목 아래에는 현재 필터 기준으로 숫자로 확인되는 관찰 한 줄이 자동으로 생성됩니다.

분석 키워드: `buldak challenge`, `spicy noodle challenge`, `shin ramyun`, `korean ramen recipe` (미국 지역, 영어 기준)

## 데이터 수집 방식

- 데이터는 저장소에 포함하지 않습니다. 앱이 YouTube Data API v3로 직접 수집합니다.
- 앱을 열었을 때 데이터가 없거나 마지막 수집 후 24시간이 지났으면 새로 검색합니다. 아무도 열지 않으면 수집하지 않습니다.
- 수집 후 30일이 지난 데이터는 자동으로 삭제합니다 (YouTube API 정책의 30일 갱신·삭제 기준).
- 수집 기준 `v2_relevance_3to30d`
  - 관련도순 검색, 수집 시점 기준 3~30일 전에 업로드된 영상
  - 키워드당 쇼츠 최대 50개 (검색 결과 최대 2페이지)
  - 대시보드는 가장 최근에 성공한 수집 1회분만 사용
- 1회 수집에 약 700~820 API 유닛이 듭니다 (하루 한도 10,000).

## 지표 정의

| 지표 | 계산 | 제외 기준 |
|---|---|---|
| 참여율 | (좋아요 + 댓글) ÷ 조회수 | 좋아요 숨김·댓글 사용 중지 영상은 0으로 넣지 않고 제외 |
| 조회수 속도 | 조회수 ÷ 업로드 후 경과 일수 | 경과 일수는 최소 1일로 처리 |
| 구독자 대비 조회수 | 조회수 ÷ 채널 구독자 수 | 구독자 비공개 채널 제외. 같은 수집 시점(없으면 가장 가까운 시점)의 구독자 수 사용 |

- 공유 수는 API가 제공하지 않아 참여율에 포함하지 않습니다.
- 요약 통계는 평균(영상별 값의 단순 평균)과 중앙값을 함께 제시합니다.
- 지표는 영상별로 가장 최근 스냅샷을 기준으로 계산합니다.

### 쇼츠 판정 기준

YouTube API에는 쇼츠 여부 필드가 없어서, YouTube 공식 기준을 길이와 화면 비율로 적용합니다.

- 정사각형 또는 세로 영상(높이 ≥ 너비)만 쇼츠입니다. 가로 영상은 길이와 상관없이 일반 영상입니다.
- 2024-10-15 이후 업로드: 180초 이하
- 2024-10-15 이전 업로드: 60초 이하
- 화면 비율은 `videos.list`의 `player.embedWidth/embedHeight`로 판단합니다.

## 한계

- **무작위 표본이 아닙니다.** 키워드 검색의 관련도 상위 영상이라 성과가 좋은 영상 쪽으로 치우쳐 있을 수 있습니다.
- **공유 수·시청 시간·비용·전환은 포함하지 않습니다.** YouTube Data API가 제공하지 않습니다.
- **광고 조회수와 자연 조회수를 구분할 수 없습니다.**
- **구독자 수는 반올림된 값입니다.** YouTube가 유효숫자 3자리로 반올림해 제공합니다.
- **구독자가 매우 적은 채널은 구독자 대비 조회수가 극단적으로 커집니다.** 그래서 크리에이터 표는 기본으로 구독자 1,000명 이상만 보여 줍니다.
- **크리에이터 표의 영상 수는 이번 검색에 잡힌 영상 수입니다.** 채널 전체 영상 수가 아닙니다.
- **쇼츠 판정은 근사치입니다.** 길이·비율 조건을 만족하지만 일반 영상으로 올라온 경우를 구분하지 못합니다.

## 실행 방법

Python 3.10 이상에서 실행합니다.

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt     # macOS/Linux: venv/bin/python
```

프로젝트 폴더에 `.env` 파일을 만들고 YouTube Data API v3 키를 넣습니다. 이 파일은 git에서 제외됩니다.

```
YOUTUBE_API_KEY=발급받은_키
```

```bash
venv\Scripts\python.exe -m streamlit run dashboard.py   # 대시보드 (데이터가 없으면 열 때 자동 수집)
venv\Scripts\python.exe collect.py search               # 명령줄에서 직접 수집
venv\Scripts\python.exe collect.py refresh              # 저장된 쇼츠의 지표만 갱신 (약 10유닛)
venv\Scripts\python.exe collect.py purge                # 30일 지난 데이터 삭제
venv\Scripts\python.exe -m pytest tests                 # 테스트
```

- 로컬에서 자동 수집을 끄려면 환경변수 `AUTO_COLLECT=0`을 설정합니다.
- API 키를 읽는 순서는 환경변수(`.env` 포함), 그다음 Streamlit Secrets입니다. 키 값은 화면이나 로그에 출력하지 않고, 오류 메시지에 섞이면 `***`로 가립니다.

### Streamlit Community Cloud 배포

1. 이 저장소로 새 앱을 만들고 Main file을 `dashboard.py`로 지정합니다.
2. Advanced settings → Secrets에 다음을 입력합니다.
   ```toml
   YOUTUBE_API_KEY = "발급받은_키"
   ```
3. Google Cloud Console에서 이 키의 API 제한을 YouTube Data API v3로만 설정하기를 권장합니다.

## 폴더 구조

```
├── dashboard.py        # Streamlit 대시보드 (화면만, 계산은 metrics.py)
├── metrics.py          # 지표 계산·요약·제외 집계 (대시보드·리포트 공용)
├── observations.py     # 섹션별 관찰 문장 생성 (숫자 사실만)
├── auto_collect.py     # 앱을 열 때 24시간 경과 확인 → 수집, 동시 접속 잠금, 실패 후 재시도 대기
├── collect.py          # YouTube API 수집 (search / refresh / reclassify / purge)
├── db.py               # SQLite 스키마·저장·30일 삭제
├── keys.py             # API 키 읽기 (.env → Streamlit Secrets), 키 가리기
├── config.py           # 키워드·수집 기준·판정 기준·보관 기간 설정
├── tests/              # pytest (지표 계산, 관찰 문장, 자동 수집·삭제 로직)
├── requirements.txt
└── data/               # SQLite DB (git 제외, 실행 시 생성)
```
