"""`fa init` default skills: a bare name several skills share is picked, not refused (#2243, q24).

Default skills belong to roles: the hint names the role's default_skills entry and `fa init`,
which applies a change to it (#2705). `fa lanes skills` is mentioned only for a lane's fixed skills.
"""

MESSAGES = {
    "en": {
        "init_skill_picked": (
            "{name} matches {count} skills: {ids}. Setup uses {picked}. "
            "To use another copy, write its id in place of {name} under default_skills in .foragent.yml "
            "for {roles}, for example default_skills.{role}: [{example}], then run fa init again. "
            "(A lane's fixed skills are set with fa lanes skills <lane> <id> instead.)"
        ),
        "init_skill_kept_disagree": (
            "Kept .claude/skills/{folder} as it is: the default_skills entries of {roles} name different "
            "copies ({wanted}). Write the same id in each of them, then run fa init again."
        ),
        "init_skill_kept_reader": (
            "Kept .claude/skills/{folder} as it is instead of {wanted}: {readers} also load that folder, "
            "and replacing it would change their skill too."
        ),
        "init_skill_kept_changed": (
            "Kept .claude/skills/{folder} as it is instead of {wanted}: fa init did not install this copy, "
            "or it changed since. To use {wanted}, move that folder away, then run fa init again."
        ),
    },
    "ko": {
        "init_skill_picked": (
            "{name} 이름의 스킬이 {count}개예요: {ids}. 설정은 {picked}를 써요. "
            "다른 사본을 쓰려면 .foragent.yml의 default_skills에서 {roles} 역할의 {name} 대신 그 id를 적고 "
            "fa init 명령을 다시 실행하세요. 예: default_skills.{role}: [{example}] "
            "(레인의 고정 스킬은 fa lanes skills <lane> <id> 명령으로 바꿔요.)"
        ),
        "init_skill_kept_disagree": (
            ".claude/skills/{folder}는 그대로 뒀어요. {roles} 역할의 default_skills 항목이 서로 다른 사본({wanted})을 "
            "가리켜요. 모두 같은 id로 적고 fa init 명령을 다시 실행하세요."
        ),
        "init_skill_kept_reader": (
            ".claude/skills/{folder}는 {wanted}로 바꾸지 않고 그대로 뒀어요. {readers}도 이 폴더를 읽어서, "
            "바꾸면 그쪽 스킬도 바뀌어요."
        ),
        "init_skill_kept_changed": (
            ".claude/skills/{folder}는 {wanted}로 바꾸지 않고 그대로 뒀어요. fa init이 설치한 사본이 아니거나 "
            "설치 뒤 바뀌었어요. {wanted}를 쓰려면 그 폴더를 다른 곳으로 옮기고 fa init 명령을 다시 실행하세요."
        ),
    },
}
