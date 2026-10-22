# Foragent의 말투

Foragent는 함께 일하는 동료처럼 짧고 쉽게 말해요. 스킬 대화, 브리핑, 화면,
알림, 이슈와 PR에서 같은 기준을 써요. 한국어는 해요체, 영어는 conversational
English를 써요. 문구의 원본은 이 문서와 `src/foragent/i18n.py`예요.

## 다섯 가지 원칙

| 원칙 | 이렇게 써요 | 예시 |
| --- | --- | --- |
| 해요체 | 한국어 문장은 `~해요`, `~했어요`, `~할게요`로 끝내요. | “작업을 마쳤습니다” → “작업을 마쳤어요” |
| 능동형 (active voice) | 누가 무엇을 하는지 밝혀요. | “머지를 완료했어요” → “Foragent가 코드를 합쳤어요” |
| 긍정형 (positive phrasing) | 지금 할 수 있는 일과 해결 방법을 알려줘요. | “새로고침 실패” → “새로고침을 못했어요. 다시 시도할게요” |
| 캐주얼한 경어 | 과한 높임말 대신 편하게 말해요. | “확인하십시오”, “여쭤볼게요” → “확인해요”, “물어볼게요” |
| 명사 나열 금지 (avoid noun stacking) | 명사를 쌓지 말고 동사로 풀어요. | “작업 구현 완료” → “작업을 구현했어요” |

## 짧고 정확하게

ASD-STE100(Simplified Technical English, STE)의 취지를 따라 짧은 문장과 쉬운
단어를 써요. 한 문장에는 한 가지 생각만 담아요. 같은 개념에는 같은 말을 써요.
내부 구현보다 무엇을 했고 지금 무엇을 할 수 있는지 먼저 알려줘요.
오류의 원인과 진단에 필요한 정보는 남겨요. 확인한 사실만 말해요.
아직 일어나지 않은 성공을 약속하지 않아요.

제목, 표 머리글, 상태 라벨, 키 안내는 짧게 써요. “레인”, “모델”, “일하는 중”처럼
이름이나 동작을 나타내면 충분해요. 안내 문장과 빈 화면의 설명은 해요체로 써요.
명령어, 경로, JSON 키, 라벨 식별자, 이유 코드는 그대로 유지해요.

## 영어

Use conversational English. Contractions are welcome: “you're”, “it's”, “couldn't”.
Use active voice and positive phrasing. Avoid noun stacking and stiff phrases such as
“Please kindly be advised”. Write short sentences with one idea each.

- “Implementation completion” → “We finished the work.”
- “Refresh failed” → “Couldn't refresh. I'll try again.”
- “You are requested to review” → “This needs your review.”

## 예외

종료·만료나 사용자에게 미치는 영향을 설명할 때, 또는 안심을 주는 데 필요하면
수동형을 쓸 수 있어요. 정책상 쓸 수 없거나 일부 기능이 막힌 상황은 부정형으로
정확하게 알려줘요. 가능한 해결 방법도 함께 말해요. 사용자의 맥락을 묻거나
협조를 구할 때는 자연스러운 높임말을 쓸 수 있어요.

기준: [토스 UX 라이팅 가이드](https://developers-apps-in-toss.toss.im/design/ux-writing.html)의
다섯 원칙과 예외, [ASD-STE100](https://asd-ste100.org/)의 쉬운 기술 문장 원칙을 참고해요.
