import unittest
from unittest.mock import patch

from app import main


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
                )
            )

        mock_agent.assert_called_once_with(
            "personal",
            "Help me plan my career",
            user_profile=profile
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["response"],
            "Here is your personalized plan."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
