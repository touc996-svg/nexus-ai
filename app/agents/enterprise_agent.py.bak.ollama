
from openai import OpenAI
from app.core.config import settings


class EnterpriseAgent:

    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def run(self, message: str):

        if not message.strip():
            return {"error": "Message cannot be empty"}

        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
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
