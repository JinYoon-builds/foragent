"""PR body overflow notes."""

MESSAGES = {
    "en": {
        "pr_body_block_removed": "_{label}: removed to fit GitHub's limit. Full text: PR comment \"{header}\", local copy {path}_",
        "pr_body_full_text": "Full PR body {digest}",
        "pr_body_too_large": "This PR's required content does not fit GitHub's limit, so nothing was sent.",
    },
    "ko": {
        "pr_body_block_removed": "_{label}: GitHub 제한에 맞추려고 뺐어요. 전체 원문(full text): PR 댓글 \"{header}\", 로컬 사본 {path}_",
        "pr_body_full_text": "Full PR body {digest}",
        "pr_body_too_large": "이 PR의 필수 내용이 GitHub 제한을 넘어 아무것도 보내지 않았어요.",
    },
}
