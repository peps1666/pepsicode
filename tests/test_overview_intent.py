import pytest

from pepsicode.core.task_control import classify_intent


@pytest.mark.parametrize(
    "text",
    [
        "项目里有啥",
        "这个项目有啥？",
        "帮我看看项目里有什么",
        "请介绍一下这个项目是做什么的。",
        "当前项目有哪些文件",
        "这个项目都有什么功能？",
    ],
)
def test_project_overviews_are_read_only_answers(text):
    intent = classify_intent(text)
    assert intent["kind"] == "answer"
    assert intent["locked"] and intent["read_only_request"]


@pytest.mark.parametrize(
    "text",
    [
        "帮我看看项目里有什么，然后修复漏洞",
        "项目里有什么并运行测试",
        "先解释原因再修改首页",
        "帮我支持 CSV 导出",
    ],
)
def test_overview_and_explicit_action_are_not_locked_read_only(text):
    intent = classify_intent(text)
    assert intent["kind"] == "change"
    assert not intent["read_only_request"]


def test_ambiguous_project_mention_is_not_forced_read_only():
    assert not classify_intent("这个项目")["read_only_request"]
