
from openai import OpenAI
from app.core.config import settings


class CybersecurityAgent:

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
                            "You are NEXUS AI Cybersecurity Agent. "
                            "Analyze security events, suspicious logins, "
                            "threats, vulnerabilities and system logs. "
                            "Identify risks and recommend practical "
                            "defensive security actions. "
                            "Never claim an attack is confirmed without evidence."
                        )
                    },
                    {"role": "user", "content": message}
                ]
            )

            return {
                "agent": "Cybersecurity AI Agent",
                "domain": "cybersecurity",
                "message": message,
                "response": response.choices[0].message.content,
                "status": "success"
            }

        except Exception as e:
            return {
                "error": str(e),
                "status": "failed"
            }


cybersecurity_agent = CybersecurityAgent()
