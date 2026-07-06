# 강의계획서 자동 생성 Flask 서버
import os
import json
import csv
import re
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from google import genai
from tavily import TavilyClient

load_dotenv()

app = Flask(__name__)

# ─────────────────────────────────────────
# 설정
# ─────────────────────────────────────────

# Gemini 모델 폴백 체인 
MODELS = [
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

client  = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
tavily  = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

RESULTS_DIR = Path("lecture_results")
RESULTS_DIR.mkdir(exist_ok=True)

LOG_FILE      = "subagent_log.csv"
TEMPLATE_FILE = "lecture_plan_template.md"

# ─────────────────────────────────────────
# 공통 유틸리티
# ─────────────────────────────────────────

def log_start(name: str) -> float:
    """서브에이전트 시작 시각을 subagent_log.csv에 기록하고 시작 시각을 반환한다."""
    start = time.time()
    _write_log(name, "START", "")
    return start


def log_end(name: str, start: float) -> None:
    """서브에이전트 종료 시각과 소요 시간을 subagent_log.csv에 기록한다."""
    duration = f"{time.time() - start:.1f}s"
    _write_log(name, "END", duration)


def _write_log(name: str, event: str, duration: str) -> None:
    file_exists = Path(LOG_FILE).exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "event", "subagent_name", "duration_sec"])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            event,
            name,
            duration,
        ])


def call_with_retry(prompt: str, max_retries: int = 3) -> str:
    """모델 폴백 체인으로 Gemini를 호출한다. 429/503 에러 시 다음 모델로 전환."""
    for model in MODELS:
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                )
                return response.text.strip()
            except Exception as e:
                err = str(e)
                if "429" in err or "503" in err:
                    wait = 2 ** attempt
                    print(f"[{model}] {err} — {wait}s 대기 후 재시도")
                    time.sleep(wait)
                else:
                    print(f"[{model}] 오류: {err}")
                    break  # 다음 모델로
    raise RuntimeError("모든 모델에서 호출 실패")


def parse_json_response(text: str) -> dict:
    """LLM 응답에서 JSON 블록을 추출해 파싱한다."""
    # ```json ... ``` 블록 또는 순수 JSON 처리
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    json_str = match.group(1) if match else text
    return json.loads(json_str.strip())


# ─────────────────────────────────────────
# /plan — 서브토픽 3~5개 분해
# ─────────────────────────────────────────

@app.route("/plan", methods=["POST"])
def plan():
    start = log_start("plan")
    try:
        data = request.json
        topic            = data["topic"]
        audience_level   = data["audience_level"]
        duration         = data["duration"]
        delivery_method  = data["delivery_method"]
        platform_tools   = data["platform_tools"]
        constraints      = data["constraints"]

        prompt = f"""
너는 강의 기획 전문가다. 아래 정보를 바탕으로 강의를 구성하는 핵심 서브토픽 3~5개를 뽑아라.
각 서브토픽은 인터넷 검색 키워드로 바로 쓸 수 있는 한 문장으로 작성한다.
platform_tools와 constraints를 반드시 고려해 현실적인 서브토픽을 선정한다.

주제: {topic}
대상자/수준: {audience_level}
일수/시간: {duration}
강의방식: {delivery_method}
플랫폼/도구: {platform_tools}
제약조건: {constraints}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "subtopics": ["서브토픽1", "서브토픽2", "서브토픽3"]
}}
"""
        result = parse_json_response(call_with_retry(prompt))
        # 입력 필드를 함께 전달해 다음 노드가 사용할 수 있도록 함
        return jsonify({**data, **result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("plan", start)


# ─────────────────────────────────────────
# /research — 서브토픽 1개 검색 + 요약
# ─────────────────────────────────────────

@app.route("/research", methods=["POST"])
def research():
    start = log_start("research")
    try:
        data     = request.json
        subtopic = data["subtopic"]

        # 교육 키워드 조합 쿼리 (Level 4.1 researcher 전략과 동일)
        queries = [
            f"{subtopic} 강의",
            f"{subtopic} 교육 커리큘럼",
            f"{subtopic} 부트캠프",
        ]

        # Tavily 검색 (위키/논문/개인 블로그 제외)
        search_results = tavily.search(
            query=queries[0],
            max_results=5,
            exclude_domains=["wikipedia.org", "namu.wiki"],
        )
        contents = "\n\n".join([
            f"제목: {r['title']}\n내용: {r['content']}"
            for r in search_results.get("results", [])
        ])

        prompt = f"""
아래는 "{subtopic}"에 대한 검색 결과다.
강의계획서 작성에 직접 활용할 수 있는 내용만 5~8문장으로 요약해라.
- 어떤 개념/실습을 어떤 순서로 가르치는가
- 어떤 툴/환경을 사용하는가
- 수강 후 할 수 있는 것(학습 결과)은 무엇인가

검색 결과:
{contents}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "subtopic": "{subtopic}",
  "search_queries": {json.dumps(queries[:2], ensure_ascii=False)},
  "summary": "요약 내용"
}}
"""
        result = parse_json_response(call_with_retry(prompt))
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("research", start)


# ─────────────────────────────────────────
# /write — 초안 작성 / 재작업
# ─────────────────────────────────────────

@app.route("/write", methods=["POST"])
def write():
    mode = request.json.get("mode", "initial")
    start = log_start(f"write_{mode}")
    try:
        data = request.json

        # 템플릿 로드
        template = ""
        if Path(TEMPLATE_FILE).exists():
            template = Path(TEMPLATE_FILE).read_text(encoding="utf-8")

        if mode == "rework":
            # 재작업: 직전 피드백 항목만 수정
            prompt = f"""
너는 강의계획서 작성 전문가다.
아래 피드백에서 지적된 항목만 정확히 수정하고 나머지는 그대로 유지해라.

직전 피드백:
{data['feedback']}

현재 초안:
{data['draft']}

출력은 완성된 마크다운 본문 전체만 출력한다 (설명 문구 없이).
"""
        else:
            # 최초 작성
            research_summaries = "\n\n".join([
                f"[{r['subtopic']}]\n{r['summary']}"
                for r in data.get("research_results", [])
            ])
            prompt = f"""
너는 강의계획서 작성 전문가다.
아래 서식과 정보를 바탕으로 완성된 강의계획서를 작성해라.

[서식]
{template}

[입력 정보]
주제: {data['topic']}
대상자/수준: {data['audience_level']}
일수/시간: {data['duration']}
강의방식: {data['delivery_method']}
플랫폼/도구: {data['platform_tools']}
제약조건: {data['constraints']}

[리서치 요약]
{research_summaries}

시간 배치 규칙 (반드시 준수):
- 1일 = 8시간(480분) 기준
- 점심시간 60분 포함
- 오전/오후 각 10분 휴식 최소 1회씩 (하루 최소 2회)
- 각 세션은 90분 이하
- 영어 약어는 최초 등장 시 전체 단어 병기 (예: AI(Artificial Intelligence))
- platform_tools에 명시된 도구가 세션 내용에 자연스럽게 반영
- constraints를 반드시 반영

출력은 완성된 마크다운 본문 전체만 출력한다 (설명 문구 없이).
"""

        draft = call_with_retry(prompt)
        return jsonify({**data, "draft": draft, "mode": mode})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end(f"write_{mode}", start)


# ─────────────────────────────────────────
# /review_content — 내용 충실도 검토
# ─────────────────────────────────────────

@app.route("/review_content", methods=["POST"])
def review_content():
    start = log_start("review_content")
    try:
        data = request.json
        prompt = f"""
너는 강의 내용 검토 전문가다.
아래 강의계획서 초안을 검토해라.
학습 목표와 커리큘럼/세션 내용이 논리적으로 연결되는지,
내용 누락이나 모순이 없는지, 제약조건이 반영됐는지만 평가한다
(시간 배치나 난이도는 평가하지 않는다).

제약조건: {data.get('constraints', '없음')}

초안:
{data['draft']}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "category": "content",
  "score": 0에서 100 사이 정수,
  "feedback": "구체적인 문제점. 문제 없으면 문제 없음"
}}
"""
        result = parse_json_response(call_with_retry(prompt))
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("review_content", start)


# ─────────────────────────────────────────
# /review_time — 시간 배치 규칙 검토
# ─────────────────────────────────────────

@app.route("/review_time", methods=["POST"])
def review_time():
    start = log_start("review_time")
    try:
        data = request.json
        prompt = f"""
너는 시간 배분 검토 전문가다.
아래 강의계획서 초안에서 시간 배치 규칙만 확인한다.

체크리스트:
- 일자별 세션 합계 + 점심 60분 + 휴식 20분 이상 = 480분인가?
- 모든 세션이 90분 이하인가?
- 오전/오후 각 최소 1회, 하루 최소 2회 휴식이 있는가?
- 점심시간이 60분으로 명시되어 있는가?

초안:
{data['draft']}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "category": "time_allocation",
  "score": 0에서 100 사이 정수,
  "feedback": "위반 항목을 구체적으로. 문제 없으면 문제 없음"
}}
"""
        result = parse_json_response(call_with_retry(prompt))
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("review_time", start)


# ─────────────────────────────────────────
# /review_difficulty — 난이도 적합성 검토
# ─────────────────────────────────────────

@app.route("/review_difficulty", methods=["POST"])
def review_difficulty():
    start = log_start("review_difficulty")
    try:
        data = request.json
        prompt = f"""
너는 난이도 적합성 검토 전문가다.
아래 강의계획서 초안이 대상자 수준에 적합한지만 평가한다.
내용 난이도가 대상자 수준과 맞는지,
실습이 있다면 대상자가 따라갈 수 있는 수준인지만 확인한다.

대상자/수준: {data.get('audience_level', '미지정')}

초안:
{data['draft']}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "category": "difficulty",
  "score": 0에서 100 사이 정수,
  "feedback": "구체적인 문제점. 문제 없으면 문제 없음"
}}
"""
        result = parse_json_response(call_with_retry(prompt))
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("review_difficulty", start)


# ─────────────────────────────────────────
# /review_final — 점수 종합 (모드1) / 재검토 (모드2)
# ─────────────────────────────────────────

@app.route("/review_final", methods=["POST"])
def review_final():
    data = request.json
    mode = data.get("mode", "initial")
    start = log_start(f"review_final_{mode}")
    try:
        if mode == "initial":
            # 모드 1: 세 reviewer 결과 종합
            reviews = data["reviews"]  # list of {category, score, feedback}
            scores  = [r["score"] for r in reviews]
            avg     = int(sum(scores) / len(scores))
            passed  = avg >= 80    # 기존 통과 기준
            # passed  = avg >= 30   # 테스트용 임시 변경

            # 80점 미만 항목의 피드백만 수집
            failed_feedbacks = [
                f"[{r['category']}] {r['feedback']}"
                for r in reviews
                if r["score"] < 80 and r["feedback"] != "문제 없음"
            ]
            feedback = "\n".join(failed_feedbacks) if failed_feedbacks else "문제 없음"

            return jsonify({
                **data,
                "mode": "initial",
                "review_score": avg,
                "pass": passed,
                "feedback": feedback,
                "reviews": reviews,
            })

        else:
            # 모드 2: 재검토 — 직전 피드백이 반영됐는지만 확인
            prompt = f"""
아래 피드백의 각 지적 사항이 새 초안에 실제로 반영됐는지만 확인해라.
모두 반영됐으면 pass=true, 하나라도 안 됐으면 pass=false.

직전 피드백:
{data['feedback']}

새 초안:
{data['draft']}

출력은 다음 JSON 형식으로만 응답한다 (다른 텍스트 포함 금지):
{{
  "mode": "rework_check",
  "pass": true 또는 false,
  "feedback": "반영 안 된 항목. 모두 반영됐으면 모두 반영됨"
}}
"""
            result = parse_json_response(call_with_retry(prompt))
            return jsonify({**data, **result})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end(f"review_final_{mode}", start)


# ─────────────────────────────────────────
# /save — 최종 강의계획서 + pipeline_log.md 저장
# ─────────────────────────────────────────

@app.route("/save", methods=["POST"])
def save():
    start = log_start("save")
    try:
        data     = request.json
        draft    = data["draft"]
        summary  = data.get("pipeline_summary", {})
        now      = datetime.now()

        # 파일 1: 최종 강의계획서
        final_path = RESULTS_DIR / "lecture_plan_final.md"
        final_path.write_text(draft, encoding="utf-8")

        # 실패본 저장 (재작업이 있었던 경우)
        rework_count = summary.get("rework_count", 0)
        if rework_count > 0 and "failed_draft" in data:
            failed_path = RESULTS_DIR / f"draft_v{rework_count}_failed.md"
            failed_path.write_text(data["failed_draft"], encoding="utf-8")

        # 파일 2: pipeline_log.md
        log_ts   = now.strftime("%Y%m%d_%H%M%S")
        log_path = RESULTS_DIR / f"pipeline_log_{log_ts}.md"

        agents_log = "\n".join([
            f"{i+1}. {step}" for i, step in
            enumerate(summary.get("steps", []))
        ]) or "(기록 없음)"

        generated_files = [str(final_path)]
        if rework_count > 0:
            generated_files.append(str(RESULTS_DIR / f"draft_v{rework_count}_failed.md"))
        generated_files.append(str(log_path))

        log_content = f"""=== 강의계획서 생성 실행 로그 ===
실행일시: {now.strftime("%Y-%m-%d %H:%M:%S")}

[입력값]
- topic:           {summary.get("topic", data.get("topic", ""))}
- audience_level:  {summary.get("audience_level", data.get("audience_level", ""))}
- duration:        {summary.get("duration", data.get("duration", ""))}
- delivery_method: {summary.get("delivery_method", data.get("delivery_method", ""))}
- platform_tools:  {summary.get("platform_tools", data.get("platform_tools", ""))}
- constraints:     {summary.get("constraints", data.get("constraints", ""))}

[서브에이전트 실행 순서]
{agents_log}

[생성 파일]
{chr(10).join("- " + f for f in generated_files)}

[최종 결과]
- 최종 점수:     {summary.get("review_score", "-")}점
- pass 여부:     {summary.get("pass", "-")}
- rework 횟수:   {rework_count}회
- HITL 발생:     {summary.get("hitl_triggered", False)}
- 사람 결정:     {summary.get("human_decision", "-")}
================================
"""
        log_path.write_text(log_content, encoding="utf-8")

        return jsonify({
            "save_path": str(final_path),
            "log_path":  str(log_path),
            "success":   True,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        log_end("save", start)


# ─────────────────────────────────────────
# 서버 실행
# ─────────────────────────────────────────

if __name__ == "__main__":
    print("Flask 서버 시작: http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=True)