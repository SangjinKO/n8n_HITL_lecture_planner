# n8n-HITL-lecture-planner
> Multi-Agent AI Workflow for Lecture Plan Generation — n8n · Flask · Gemini · Tavily · HITL

---

## Why

강의계획서 작성은 강사에게 반복적이고 시간이 많이 드는 작업이다. 주제 조사, 커리큘럼 설계, 시간 배분 검토, 난이도 검증이 순서대로 필요한 이 과정을 멀티에이전트 파이프라인으로 자동화했다.

단순 자동화가 아니라 **품질 미달 시 사람이 개입하는 HITL(Human-in-the-Loop) 구조**를 추가했다. AI가 생성한 결과물의 품질 판단을 AI에게만 맡기지 않고, 기준 미달 시 Slack으로 알림을 보내 사람이 "재작업" 또는 "현재 버전 저장"을 직접 결정하도록 설계했다.

핵심 아키텍처: n8n이 전체 멀티에이전트 흐름을 제어하고, Gemini가 각 역할(기획/조사/작성/검토)을 담당하며, Tavily가 실시간 웹 검색을 처리한다.

---

## Architecture

```
Webhook (6개 필드)
      ↓
Input Gate (Field Check) — 필드 누락 시 즉시 에러 반환
      ↓
Plan — 서브토픽 4개 분해
      ↓
Split Out → Research(병렬) → Aggregate    ← Round 1: 순차
      ↓
Write — 템플릿 기반 초안 작성              ← Round 2: 순차
      ↓
Review-Content ─┐
Review-Time    ─┼──→ Merge-Review → Parsing → Review-Final  ← Round 3: 병렬 (3개 동시)
Review-Difficulty┘
      ↓
If-Pass [pass=true]
  true  → Save-Auto → 종료
  false → If-False-Slack → Wait (HITL)
               ↓
           If-Rework
             rework → Re-Write → Re-Review-Final → Save-HITL
             save   → Save-HITL
```

### n8n 노드 대응표 (Flask 엔드포인트)

| n8n 노드 | Flask 엔드포인트 | 역할 |
|---|---|---|
| Plan | `/plan` | Gemini로 서브토픽 3~5개 분해 |
| Research(병렬) | `/research` | Tavily 검색 + Gemini 요약 |
| Write | `/write` | 강의계획서 초안 작성 |
| Review-Content | `/review_content` | 내용 충실도 검토 |
| Review-Time | `/review_time` | 시간 배치 규칙 검토 |
| Review-Difficulty | `/review_difficulty` | 난이도 적합성 검토 |
| Review-Final | `/review_final` | 점수 종합 (모드1) / 피드백 반영 확인 (모드2) |
| Save-Auto / Save-HITL | `/save` | 최종 파일 + 로그 저장 |

---

## Key Design Decisions

**HITL 게이트 — 자동 재작업 대신 사람에게 판단 위임**

강의계획서는 정량적 점수만으로 품질을 판단하기 어렵다. AI 점수가 80점 미달이어도 의뢰인이 "이 정도면 충분하다"고 판단할 수 있고, 그 반대도 있다. 자동 재작업 루프 대신 Slack 알림 → n8n Wait 노드로 사람이 직접 결정하는 구조를 선택했다.

**Research 순차 처리 — n8n 병렬의 한계**

n8n 분기(선 여러 개 연결)는 "같은 데이터를 여러 경로로 동시에 보내는 것"이다. Research 노드를 N개 만들어 병렬 연결하면 각 노드에 서브토픽 전부가 들어가 N² 호출이 발생한다. Split Out → 단일 HTTP Request 노드(Execute Once OFF) 패턴으로 해결했다.

고정 N개 병렬(reviewer 3개)은 노드 분기 + Merge 패턴으로 정상 동작한다.

**Gemini 모델 폴백 체인**

무료 API 티어의 429/503 에러 대응으로 폴백 체인을 구성했다.

```python
MODELS = [
    "gemini-3.1-flash-lite",   # 우선 사용 (무료 토큰 가장 많음)
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]
```

**Research 검색 전략 — 교육 키워드 조합**

서브토픽을 그대로 검색하면 위키피디아 정의나 논문이 걸린다. `{서브토픽} 강의`, `{서브토픽} 교육 커리큘럼`, `{서브토픽} 부트캠프` 조합으로 쿼리를 만들고 wikipedia.org, namu.wiki를 제외했다.

**JSON.stringify() — n8n에서 여러 줄 텍스트 전달**

`draft`, `feedback`처럼 줄바꿈이 포함된 필드를 n8n JSON Body에 `"{{ $json.field }}"` 형태로 넣으면 Bad control character 오류가 발생한다. 이 세 필드는 항상 `{{ JSON.stringify($json.field) }}`로 처리한다.

Slack 메시지처럼 여러 필드를 조합하는 경우: Specify Body → Using Fields 방식으로 변경하면 각 필드가 독립 표현식으로 평가된다.

---

## Tech Stack

| 항목 | 내용 |
|---|---|
| Orchestration | n8n (Docker) |
| LLM | Gemini 3.1 Flash Lite (무료 API, gemini-2.5-flash-lite / gemini-2.5-flash 폴백) |
| 웹 검색 | Tavily Search API |
| 백엔드 서버 | Flask (Python) |
| HITL 알림 | Slack Incoming Webhook |
| 로깅 | subagent_log.csv (엔드포인트 START/END) + pipeline_log_{ts}.md (실행 요약) |

---

## Repository Structure

```
multi-agent-lecture-planner/
├── server.py                   # Flask 서버 — 8개 엔드포인트
├── requirements.txt
├── n8n_workflow.json            # n8n 워크플로우 (import 가능)
├── lecture_plan_template.md     # 강의계획서 출력 형식
├── lecture_results/
│   ├── lecture_plan_final.md   # 최종 산출물
│   ├── draft_v1_failed.md      # 재작업 발생 시 실패본
│   └── pipeline_log_*.md       # 실행 요약 로그
├── subagent_log.csv            # 엔드포인트 실행 기록
└── .env.example
```

---

## How to Run

### 환경 설정

```bash
cp .env.example .env
# .env에 GEMINI_API_KEY, TAVILY_API_KEY, SLACK_WEBHOOK_URL 입력
```

```bash
pip install flask python-dotenv google-genai tavily-python requests
```

### Flask 서버 실행

```bash
python3 server.py
# http://localhost:5001
```

### n8n 실행 (Docker)

```bash
docker run -it --rm \
  --name n8n \
  -p 5678:5678 \
  -v ~/.n8n:/home/node/.n8n \
  n8nio/n8n
```

n8n → Import Workflow → `n8n_workflow.json` 업로드 후 활성화.

### 실행 테스트

```bash
curl -X POST http://localhost:5678/webhook-test/lecture-trigger \
  -H "Content-Type: application/json" \
  -d '{
    "topic": "업무에 바로 쓰는 AI 활용법",
    "audience_level": "비개발자 직장인 초급",
    "duration": "1일 8시간",
    "delivery_method": "강의+실습 병행",
    "platform_tools": "ChatGPT, Notion",
    "constraints": "프로젝터 없음, 노트북 1인 1대"
  }'
```

점수 미달 시 Slack 메시지 수신 → 터미널에서 curl로 재개:

```bash
# Slack 메시지의 curl 명령 복사 후 실행
curl -X POST "http://localhost:5678/webhook-waiting/{id}?signature=..." \
  -H "Content-Type: application/json" \
  -d '{"decision": "rework"}'   # 또는 "save"
```

---

## Known Issues & Lessons

**Execute Once 기본값 주의**

Split Out 뒤에 오는 HTTP Request 노드의 Execute Once가 ON이면, 아이템 수와 관계없이 첫 번째 아이템으로만 실행하고 결과를 복제한다. Settings 탭에서 반드시 OFF로 설정해야 한다.

**n8n에서 동적 N개 병렬은 복잡하다**

Split Out 뒤 HTTP Request 노드를 N개 병렬 연결하면 각 노드에 전체 아이템이 들어가 N² 호출이 발생한다. 동적 N개 진짜 병렬은 Execute Workflow 서브워크플로우 패턴이 필요하지만 구조가 복잡해진다. LangGraph 버전에서 `Send` API로 동일한 동적 병렬을 3줄로 구현한 것과 대비된다.

**Wait 노드는 GET이 아닌 POST로 재개해야 한다**

Slack 메시지의 resume URL을 브라우저에서 클릭하면 GET 요청이 전송되어 404가 발생한다. n8n Wait 노드는 POST만 수락하며, curl로 직접 전송해야 한다.

**Slack 메시지 텍스트 조립은 Using Fields 방식으로**

feedback 필드는 `[content]`, `[time_allocation]`, `[difficulty]` 항목이 줄바꿈으로 구분된 여러 줄 텍스트다. JSON Body (Using JSON 모드)에 직접 넣으면 Bad control character 오류가 발생한다. Specify Body를 Using Fields로 바꾸면 각 파라미터가 독립 표현식으로 평가되어 줄바꿈이 정상 처리된다.
