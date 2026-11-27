import unittest
from unittest.mock import patch

from app import main
from app.security.identity import UNAUTHENTICATED


class TestChatPersonalizationFlow(unittest.TestCase):

    def test_chat_forwards_saved_profile_to_personal_agent(self):
        profile = {
            "goals": ["Build an AI assistant"],
            "skills": {
                "python": "beginner",
                "machine_learning": "intermediate"
            },
            "career_direction": "AI/ML engineering"
        }

        understanding = {
            "intent_type": "agent_task",
            "agent": "personal",
            "response": "",
            "clarifying_question": None,
            "understood_request": "Plan my career"
        }

        agent_result = {
            "agent": "Personal Assistant Agent",
            "response": "Here is your personalized plan.",
            "status": "success"
        }

        with (
            patch.object(main, "DEMO_MODE", False),
            patch.object(
                main.memory, "get_history", return_value=[]
            ),
            patch.object(
                main.memory,
                "get_profile",
                return_value={"profile": profile}
            ),
            patch.object(main.memory, "add_message"),
            patch.object(
                main.conversation_engine,
                "understand",
                return_value=understanding
            ),
            patch.object(
                main.nexus,
                "run_agent",
                return_value=agent_result
            ) as mock_agent
        ):
            result = main.chat(
                main.ChatRequest(
                    message="Help me plan my career",
                    session_id="personalization-test",
                    user_id="nexus-personalization-test"
                ),
                principal=UNAUTHENTICATED,
            )

        mock_agent.assert_called_once_with(
            "personal",
            "Help me plan my career",
            internal=False,
            user_profile=profile
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["response"],
            "Here is your personalized plan."
        )


class TestChatRetrievedContextLength(unittest.TestCase):
    """Regression: a max-length message plus retrieved context must not be rejected."""

    def test_long_message_with_retrieved_context_reaches_the_agent(self):
        seen = {}

        class StubAgent:
            def run(self, message):
                seen["length"] = len(message)
                return {"response": "ok", "status": "success"}

        understanding = {
            "intent_type": "agent_task",
            "agent": "enterprise",
            "response": "",
            "clarifying_question": None,
            "understood_request": "analysis",
        }
        retrieved = [{"source": "doc.txt", "text": "x" * 9000}]

        with (
            patch.object(main, "DEMO_MODE", False),
            patch.object(main.memory, "get_history", return_value=[]),
            patch.object(main.memory, "get_profile", return_value={"profile": {}}),
            patch.object(main.memory, "add_message"),
            patch.object(main.rag, "search", return_value=retrieved),
            patch.object(main.conversation_engine, "understand", return_value=understanding),
            patch.dict(main.nexus.agents, {"enterprise": StubAgent()}),
        ):
            result = main.chat(
                main.ChatRequest(message="a" * 5000, session_id="ctx-len", user_id="ctx-len-user"),
                principal=UNAUTHENTICATED,
            )

        self.assertEqual(result["status"], "completed", result.get("response"))
        self.assertGreater(seen["length"], 5000)
        self.assertLessEqual(seen["length"], 15000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
