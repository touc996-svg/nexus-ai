import json

from app.core.llm import client, MODEL


class ProfileLearning:
    ALLOWED_FIELDS = {
        "goals",
        "skills",
        "preferences",
        "professional_background",
        "career_direction",
    }

    def extract_updates(self, message, existing_profile=None):
        """
        Extract explicit, useful profile updates from a user message.
        Does not save anything by itself.
        """

        existing_profile = existing_profile or {}

        system = """
You are NEXUS AI's Profile Learning Engine.

Your task is to identify durable personal and professional information
that the user explicitly shares in their current message.

Allowed profile fields:
- goals: long-term or clearly stated goals
- skills: explicitly stated skills and proficiency levels
- preferences: communication, learning, or work preferences
- professional_background: education, employment, certifications,
  and professional experience
- career_direction: explicitly stated career interests or direction

RULES:
1. Extract only information explicitly stated by the user.
2. Never infer qualifications, skills, experience, or achievements.
3. Do not save temporary circumstances, casual conversation,
   hypothetical statements, or questions as profile facts.
4. Do not copy information from the existing profile as new information.
5. If the user explicitly corrects an existing fact, return the correction.
6. If information is ambiguous, do not include it.
7. Preserve the user's original meaning.
8. Return empty updates when nothing meaningful should be saved.

Return ONLY valid JSON in this exact structure:
{
    "updates": {
        "goals": [],
        "skills": {},
        "preferences": {},
        "professional_background": "",
        "career_direction": ""
    }
}

Include only fields that have actual updates.
Do not return explanations or Markdown.
"""

        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Existing profile for reference:\n"
                    + json.dumps(existing_profile, ensure_ascii=False)
                    + "\n\nCurrent user message:\n"
                    + message
                ),
            },
        ]

        try:
            result = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0,
            )

            raw = result.choices[0].message.content or ""
            data = json.loads(raw)

            if not isinstance(data, dict):
                return {}

            updates = data.get("updates", {})

            if not isinstance(updates, dict):
                return {}

            return {
                key: value
                for key, value in updates.items()
                if key in self.ALLOWED_FIELDS
                and self._valid_value(key, value)
            }

        except Exception:
            return {}

    def _valid_value(self, field, value):
        if field in {"goals"}:
            return isinstance(value, list) and bool(value)

        if field in {"skills", "preferences"}:
            return isinstance(value, dict) and bool(value)

        if field in {"professional_background", "career_direction"}:
            return isinstance(value, str) and bool(value.strip())

        return False

    def merge_updates(self, existing_profile, updates):
        """
        Merge updates without discarding unrelated saved information.
        """

        merged = dict(existing_profile or {})

        for field, new_value in updates.items():
            if field not in self.ALLOWED_FIELDS:
                continue

            old_value = merged.get(field)

            if isinstance(old_value, dict) and isinstance(new_value, dict):
                merged[field] = {**old_value, **new_value}

            elif isinstance(old_value, list) and isinstance(new_value, list):
                combined = old_value + new_value
                merged[field] = list(dict.fromkeys(combined))

            else:
                merged[field] = new_value

        return merged


profile_learning = ProfileLearning()
