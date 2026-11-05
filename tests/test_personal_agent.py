import unittest
from unittest.mock import MagicMock, patch

from app.agents.personal_agent import PersonalAgent


class TestPersonalAgentPersonalization(unittest.TestCase):

    def test_saved_profile_is_included_in_system_prompt(self):
        profile = {
            "goals": ["Build an AI assistant"],
            "skills": {
                "python": "beginner",
                "machine_learning": "intermediate"
            },
            "career_direction": "AI/ML engineering"
        }

        mock_response = MagicMock()
        mock_response.choices[0].message.content = (
            "Here is a personalized learning plan."
        )

        agent = PersonalAgent()

        with patch(
            "app.agents.personal_agent.client.chat.completions.create",
            return_value=mock_response
        ) as mock_create:

            result = agent.run(
                "Help me plan my next learning steps.",
                user_profile=profile
            )

        system_prompt = mock_create.call_args.kwargs["messages"][0]["content"]

        self.assertIn("Build an AI assistant", system_prompt)
        self.assertIn("AI/ML engineering", system_prompt)
        self.assertIn("beginner", system_prompt)
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            result["response"],
            "Here is a personalized learning plan."
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
