
from app.core.llm import client, MODEL


class CybersecurityAgent:

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
