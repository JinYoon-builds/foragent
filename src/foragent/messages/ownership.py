"""Orderer identity and ownership (D78 revision, 2026-10-30)."""

MESSAGES = {
    "en": {
        "ownership_invalid_login": "{value!r} is not a GitHub login.",
        "ownership_ambiguous": (
            "This issue has more than one owner label ({owners}). "
            "Keep one by:<login> label, then try again."),
        "ownership_unknown_factory": (
            "This factory does not know its GitHub account. Run fa init to sign in again, then try again."),
        "ownership_foreign": "#{number} is worked by {owner}; this factory is {factory}.",
        "ownership_unconfirmed": (
            "#{number} was published for an owner label it does not have, so no factory works it. "
            "Add one by:<login> label for the person who ordered it, or close it."),
        "identity_unavailable": (
            "Cannot read the GitHub account for this factory, so nothing was published. "
            "Run gh auth login (or fa init), then try again. GitHub said: {error}"),
        "identity_not_a_person": (
            "The GitHub credential belongs to {login} ({kind}), not a person. "
            "Sign in with your own account (gh auth login), then try again."),
        "identity_label_missing": (
            "GitHub created #{number} without the owner label {label}. "
            "Foragent closed it so no other factory takes it. "
            "Check that your account can add labels to this repository, then publish again."),
        "identity_closed_unlabelled": (
            "Closed by Foragent: publishing could not confirm the owner label {label} on this issue, "
            "so it was never made part of the order. Publishing the order again creates a new issue."),
        "init_identity": "GitHub account: {login}",
        "init_legacy_owner_missing": (
            "Team setup missing: issues ordered by {owners} exist, but .foragent.yml has no legacy_owner. "
            "The original factory adds legacy_owner: <its login> through a PR before this factory starts."),
        "init_identity_failed": (
            "GitHub account: unknown ({error}). Run gh auth login, then fa init again before you order work."),
    },
    "ko": {
        "ownership_invalid_login": "{value!r}은 GitHub 로그인이 아니에요.",
        "ownership_ambiguous": (
            "이 이슈에 주인 라벨이 여러 개 있어요({owners}). by:<login> 라벨을 하나만 남기고 다시 해 보세요."),
        "ownership_unknown_factory": (
            "이 공장의 GitHub 계정을 몰라요. fa init으로 다시 로그인한 뒤 다시 해 보세요."),
        "ownership_foreign": "#{number}은 {owner}의 일이에요. 이 공장은 {factory}예요.",
        "ownership_unconfirmed": (
            "#{number}은 주인 라벨이 붙어야 하는데 없어서 어느 공장도 일하지 않아요. "
            "주문한 사람의 by:<login> 라벨을 하나 붙이거나 이슈를 닫으세요."),
        "identity_unavailable": (
            "이 공장의 GitHub 계정을 읽지 못해서 아무것도 발행하지 않았어요. "
            "gh auth login(또는 fa init)을 한 뒤 다시 해 보세요. GitHub 응답: {error}"),
        "identity_not_a_person": (
            "이 GitHub 자격 증명은 사람이 아니라 {login}({kind})의 것이에요. "
            "본인 계정으로 로그인(gh auth login)한 뒤 다시 해 보세요."),
        "identity_label_missing": (
            "GitHub가 #{number}을 주인 라벨 {label} 없이 만들었어요. "
            "다른 공장이 가져가지 않게 Foragent가 닫았어요. "
            "이 저장소에 라벨을 붙일 권한이 있는지 확인한 뒤 다시 발행하세요."),
        "identity_closed_unlabelled": (
            "Foragent가 닫았어요: 발행 중에 이 이슈의 주인 라벨 {label}을 확인하지 못해서 주문에 넣지 않았어요. "
            "주문을 다시 발행하면 이슈를 새로 만들어요."),
        "init_identity": "GitHub 계정: {login}",
        "init_legacy_owner_missing": (
            "팀 설정이 빠졌어요: {owners}이 주문한 이슈가 있는데 .foragent.yml에 legacy_owner가 없어요. "
            "이 공장을 시작하기 전에 원래 공장이 PR로 legacy_owner: <그 로그인>을 넣어야 해요."),
        "init_identity_failed": (
            "GitHub 계정: 알 수 없음({error}). 일을 주문하기 전에 gh auth login을 하고 fa init을 다시 실행하세요."),
    },
}
