import json
import unittest
from unittest.mock import MagicMock, patch

from app.core.conversation import ConversationIntelligence


class TestConversationValidation(unittest.TestCase):

    def setUp(self):
        self.engine = ConversationIntelligence()

    def mock_response(self, payload):
        result = MagicMock()
        result.choices[0].message.content = json.dumps(payload)
        return result

    def test_conversation(self):
        payload = {
            "intent_type": "conversation",
            "agent": None,
            "response": "Hello! How can I help?",
            "clarifying_question": None,
            "understood_request": "Greeting"
        }
        with patch(
            "app.core.conversation.client.chat.completions.create",
            return_value=self.mock_response(payload)
        ):
            result = self.engine.understand("Hello there")
        self.assertEqual(result["intent_type"], "conversation")

    def test_agent_task(self):
        payload = {
            "intent_type": "agent_task",
            "agent": "enterprise",
            "response": "",
            "clarifying_question": None,
            "understood_request": "Create a business plan"
        }
        with patch(
            "app.core.conversation.client.chat.completions.create",
            return_value=self.mock_response(payload)
        ):
            result = self.engine.understand("Help me with my project")
        self.assertEqual(result["agent"], "enterprise")

    def test_clarification(self):
        payload = {
            "intent_type": "clarification",
            "agent": None,
            "response": "",
            "clarifying_question": "Which project do you mean?",
            "understood_request": "Project request"
        }
        with patch(
            "app.core.conversation.client.chat.completions.create",
            return_value=self.mock_response(payload)
        ):
            result = self.engine.understand("Help me with something")
        self.assertEqual(result["intent_type"], "clarification")

    def test_unsupported(self):
        payload = {
            "intent_type": "unsupported",
            "agent": None,
            "response": "I cannot perform that action.",
            "clarifying_question": None,
            "understood_request": "External action"
        }
        with patch(
            "app.core.conversation.client.chat.completions.create",
            return_value=self.mock_response(payload)
        ):
            result = self.engine.understand("Tell me something")
        self.assertEqual(result["intent_type"], "unsupported")

    def test_malformed_response_uses_fallback(self):
        result = MagicMock()
        result.choices[0].message.content = '{"intent_type": "invalid"}'

        with patch(
            "app.core.conversation.client.chat.completions.create",
            return_value=result
        ):
            response = self.engine.understand("Tell me something")

        self.assertEqual(response["intent_type"], "clarification")
        self.assertTrue(response["clarifying_question"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
