"""`fa init` GitHub preflight failures with the exact recovery command (#843).

<owner>/<repo> is a placeholder the user replaces; init never guesses it.
"""

MESSAGES = {
    "en": {
        "setup_wait_for": "Complete {prerequisite} first.",
        "init_repository_choices": (
            "Choose one way forward (nothing is created or linked until you agree):\n"
            "  New private repository: gh repo create <owner>/<repo> --private --source .\n"
            "  New public repository: gh repo create <owner>/<repo> --public --source .\n"
            "  Existing repository URL: git remote add origin https://github.com/<owner>/<repo>.git"
        ),
        "init_git_missing": (
            "{error}\n"
            "Start a git work tree, then choose how to link it:\n"
            "  git init\n{choices}"
        ),
        "init_setup_incomplete": "Setup is not complete. Do these steps first:",
        "init_setup_rerun": "Then rerun fa init.",
        "init_auth_reason_timeout": "gh did not answer the login check in time.",
        "init_auth_reason_token-invalid": "gh has a saved login, but its token is invalid or expired.",
        "init_auth_reason_network": "gh could not reach GitHub to check the login.",
        "init_origin_missing": (
            "init requires an origin remote on github.com: {error}\n"
            "Add the remote (replace <owner>/<repo> with your GitHub repository), then rerun fa init:\n"
            "{choices}"
        ),
        "init_origin_not_github": (
            "init requires an origin remote on github.com: {error}\n"
            "Point origin at GitHub (replace <owner>/<repo> with your GitHub repository), then rerun fa init:\n"
            "  git remote set-url origin https://github.com/<owner>/<repo>.git"
        ),
    },
    "ko": {
        "setup_wait_for": "{prerequisite}부터 완료해 주세요.",
        "init_repository_choices": (
            "다음 중 하나를 골라 주세요. 동의하기 전에는 만들거나 연결하지 않아요:\n"
            "  새 비공개(private) 저장소: gh repo create <owner>/<repo> --private --source .\n"
            "  새 공개(public) 저장소: gh repo create <owner>/<repo> --public --source .\n"
            "  기존 저장소 URL: git remote add origin https://github.com/<owner>/<repo>.git"
        ),
        "init_git_missing": (
            "{error}\n"
            "Git 저장소를 시작하고 연결 방법을 골라 주세요:\n"
            "  git init\n{choices}"
        ),
        "init_setup_incomplete": "설정 점검을 통과하지 못했어요. 먼저 다음 단계를 마쳐 주세요:",
        "init_setup_rerun": "그다음 fa init을 다시 실행하세요.",
        "init_auth_reason_timeout": "gh가 로그인 확인에 제때 답하지 않았어요.",
        "init_auth_reason_token-invalid": "gh에 저장된 로그인의 토큰이 잘못되었거나 만료되었어요.",
        "init_auth_reason_network": "gh가 로그인을 확인하려고 GitHub에 연결하지 못했어요.",
        "init_origin_missing": (
            "init requires an origin remote on github.com: {error}\n"
            "origin 원격을 추가하세요(<owner>/<repo>는 내 GitHub 저장소로 바꾸세요). 그다음 fa init을 다시 실행하세요:\n"
            "{choices}"
        ),
        "init_origin_not_github": (
            "init requires an origin remote on github.com: {error}\n"
            "origin이 GitHub를 가리키게 바꾸세요(<owner>/<repo>는 내 GitHub 저장소로 바꾸세요). 그다음 fa init을 다시 실행하세요:\n"
            "  git remote set-url origin https://github.com/<owner>/<repo>.git"
        ),
    },
}
