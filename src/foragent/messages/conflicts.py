"""Conflict stops after in-place resolution (no from-scratch rebuild)."""

MESSAGES = {
    "en": {
        "conflict_stop": "Merge conflicts remain after {budget} in-place resolution attempts. "
                         "The PR's work is kept; {count}/{total} of its own files conflict, "
                         "so it was not rebuilt from scratch.",
        "conflict_stop_files": "Conflicting files: {paths}",
        "conflict_marker_left": "Conflict marker left: {location}",
        "conflict_stop_checks": "In-place resolution used its {budget} attempts. No conflict markers remain, "
                                "but the checks still fail. The PR's work is kept and was not rebuilt from scratch.",
        "replay_stop": "Could not replay the PR's changes onto {target}. None of its own files conflict, "
                       "so it was not rebuilt from scratch; the work is kept on the branch.",
        "review_class_question": "For each finding, say whether it is one instance of a broader defect class "
                                 "and where else to look in the change.",
    },
    "ko": {
        "conflict_stop": "제자리 해결을 {budget}번 시도했지만 머지 충돌이 남았어요. "
                         "PR 작업은 그대로 두었어요. PR 자체 파일 {total}개 중 {count}개만 충돌해서 "
                         "처음부터 다시 만들지 않았어요",
        "conflict_stop_files": "충돌 파일: {paths}",
        "conflict_marker_left": "충돌 마커가 남음: {location}",
        "conflict_stop_checks": "제자리 해결을 {budget}번 모두 썼어요. 충돌 표시는 없어졌지만 검사가 아직 실패해요. "
                                "PR 작업은 그대로 두었고 처음부터 다시 만들지 않았어요",
        "replay_stop": "PR 변경을 {target}에 다시 적용하지 못했어요. PR 자체 파일 중 충돌한 파일이 없어서 "
                       "처음부터 다시 만들지 않았어요. 작업은 브랜치에 그대로 두었어요",
        "review_class_question": "지적마다 더 넓은 결함 종류의 한 사례인지, 변경의 어디를 더 봐야 하는지 적어 주세요.",
    },
}
