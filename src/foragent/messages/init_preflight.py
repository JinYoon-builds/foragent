"""`fa init` GitHub preflight failures with the exact recovery command (#843).

<owner>/<repo> is a placeholder the user replaces; init never guesses it.
"""

MESSAGES = {
    "en": {
        "init_gh_missing": (
            "gh auth status failed: {error}\n"
            "The GitHub CLI (gh) is not installed. Install it, then rerun fa init:\n"
            "  brew install gh"
        ),
        "init_gh_logged_out": (
            "gh auth status failed: {error}\n"
            "The GitHub CLI is not logged in. Log in, then rerun fa init:\n"
            "  gh auth login"
        ),
        "init_origin_missing": (
            "init requires an origin remote on github.com: {error}\n"
            "Add the remote (replace <owner>/<repo> with your GitHub repository), then rerun fa init:\n"
            "  git remote add origin https://github.com/<owner>/<repo>.git"
        ),
        "init_origin_not_github": (
            "init requires an origin remote on github.com: {error}\n"
            "Point origin at GitHub (replace <owner>/<repo> with your GitHub repository), then rerun fa init:\n"
            "  git remote set-url origin https://github.com/<owner>/<repo>.git"
        ),
    },
    "ko": {
        "init_gh_missing": (
            "gh auth status failed: {error}\n"
            "GitHub CLI(gh)가 설치되어 있지 않아요. 설치한 뒤 fa init을 다시 실행하세요:\n"
            "  brew install gh"
        ),
        "init_gh_logged_out": (
            "gh auth status failed: {error}\n"
            "GitHub CLI에 로그인되어 있지 않아요. 로그인한 뒤 fa init을 다시 실행하세요:\n"
            "  gh auth login"
        ),
        "init_origin_missing": (
            "init requires an origin remote on github.com: {error}\n"
            "origin 원격을 추가하세요(<owner>/<repo>는 내 GitHub 저장소로 바꾸세요). 그다음 fa init을 다시 실행하세요:\n"
            "  git remote add origin https://github.com/<owner>/<repo>.git"
        ),
        "init_origin_not_github": (
            "init requires an origin remote on github.com: {error}\n"
            "origin이 GitHub를 가리키게 바꾸세요(<owner>/<repo>는 내 GitHub 저장소로 바꾸세요). 그다음 fa init을 다시 실행하세요:\n"
            "  git remote set-url origin https://github.com/<owner>/<repo>.git"
        ),
    },
}
