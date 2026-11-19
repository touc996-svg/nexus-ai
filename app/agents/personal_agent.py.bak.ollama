
from openai import OpenAI
from app.core.config import settings


class PersonalAgent:

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
                            "You are NEXUS AI Personal Assistant. "
                            "Help users with planning, organization, "
                            "daily tasks, learning and problem solving. "
                            "Give practical, clear and helpful answers."
                        )
                    },
                    {"role": "user", "content": message}
                ]
            )

            return {
                "agent": "Personal Assistant Agent",
                "domain": "personal",
                "message": message,
                "response": response.choices[0].message.content,
                "status": "success"
            }

        except Exception as e:
            return {
                "error": str(e),
                "status": "failed"
            }


personal_agent = PersonalAgent()
