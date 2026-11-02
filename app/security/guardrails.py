class NexusGuardrails:

    MAX_MESSAGE_LENGTH = 5000

    def validate(self, message: str):
        if not isinstance(message, str):
            return False, "Message must be text"

        if not message.strip():
            return False, "Message cannot be empty"

        if len(message) > self.MAX_MESSAGE_LENGTH:
            return False, "Message exceeds maximum length"

        return True, "Message approved"


guardrails = NexusGuardrails()
