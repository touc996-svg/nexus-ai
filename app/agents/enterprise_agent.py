
from app.core.llm import client, MODEL


class EnterpriseAgent:

    def __init__(self):
        self.client = client

    def run(self, message: str):

        if not message.strip():
            return {"error": "Message cannot be empty"}

        try:
            response = self.client.chat.completions.create(
                model=MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are NEXUS AI Enterprise Agent. "
                            "Help businesses with analysis, "
                            "documents, operations and problem solving. "
                            "Give clear, professional and practical answers."
                        )
                    },
                    {"role": "user", "content": message}
                ]
            )

            return {
                "agent": "Enterprise AI Agent",
                "domain": "Enterprise",
                "message": message,
                "response": response.choices[0].message.content,
                "status": "success"
            }

        except Exception as e:
            return {
                "error": str(e),
                "status": "failed"
            }


enterprise_agent = EnterpriseAgent()
