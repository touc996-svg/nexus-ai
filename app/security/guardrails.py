class NexusGuardrails:

    MAX_MESSAGE_LENGTH = 5000
    MAX_INTERNAL_CONTEXT_LENGTH = 15000

    def validate(self, message: str, max_length=None):
        if not isinstance(message, str):
            return False, "Message must be text"

        if not message.strip():
            return False, "Message cannot be empty"

        limit = max_length or self.MAX_MESSAGE_LENGTH

        if len(message) > limit:
            return False, "Message exceeds maximum length"

        return True, "Message approved"


guardrails = NexusGuardrails()
