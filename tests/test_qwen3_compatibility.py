from unittest.mock import MagicMock
from pathlib import Path

from app.core.llm import MODEL, CHAT_COMPLETION_OPTIONS
from app.agents.enterprise_agent import EnterpriseAgent
from app.agents.personal_agent import PersonalAgent
from app.agents.cybersecurity_agent import CybersecurityAgent


def test_qwen3_reasoning_is_disabled():
    if MODEL.lower().startswith("qwen3"):
        assert CHAT_COMPLETION_OPTIONS == {
            "extra_body": {"reasoning_effort": "none"}
        }


def test_all_three_agents_forward_qwen3_options():
    for agent_class in (
        EnterpriseAgent,
        PersonalAgent,
        CybersecurityAgent,
    ):
        fake = MagicMock()
        fake.chat.completions.create.return_value.choices[0].message.content = (
            "The operational review is complete with practical next steps."
        )

        result = agent_class(llm_client=fake).run(
            "Provide an operational review."
        )

        assert result["status"] == "success"
        options = fake.chat.completions.create.call_args.kwargs["extra_body"]
        assert options["reasoning_effort"] == "none"


def test_all_five_llm_call_sites_use_shared_options():
    files = [
        "app/agents/enterprise_agent.py",
        "app/agents/personal_agent.py",
        "app/agents/cybersecurity_agent.py",
        "app/core/conversation.py",
        "app/services/profile_learning.py",
    ]

    for name in files:
        content = Path(name).read_text()
        assert "**CHAT_COMPLETION_OPTIONS," in content, name
