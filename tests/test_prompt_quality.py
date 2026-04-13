"""Test prompt quality constraints — catches bloat and conflicts."""

from sre_agent.skill_loader import ALWAYS_INCLUDE, load_skills


class TestPromptQuality:
    def test_skill_prompt_token_budget(self):
        """Each skill prompt should be under 3000 tokens."""
        skills = load_skills()
        for skill in skills.values():
            tokens = len(skill.system_prompt) // 4
            assert tokens < 3000, f"Skill '{skill.name}' prompt is {tokens} tokens (max 3000)"

    def test_no_keyword_collisions(self):
        """Two skills should not share the same keyword."""
        skills = load_skills()
        keyword_owners = {}
        collisions = []
        for skill in skills.values():
            for kw in skill.keywords:
                kw_lower = kw.lower().strip()
                if kw_lower in keyword_owners and keyword_owners[kw_lower] != skill.name:
                    collisions.append((kw_lower, keyword_owners[kw_lower], skill.name))
                keyword_owners[kw_lower] = skill.name
        assert not collisions, f"Keyword collisions between skills: {collisions[:10]}"

    def test_always_include_reasonable_size(self):
        """ALWAYS_INCLUDE should not exceed 15 tools."""
        assert len(ALWAYS_INCLUDE) <= 15, f"ALWAYS_INCLUDE has {len(ALWAYS_INCLUDE)} tools (max 15)"

    def test_every_skill_has_keywords(self):
        """Every skill must have at least 2 keywords for routing."""
        skills = load_skills()
        for skill in skills.values():
            assert len(skill.keywords) >= 2, f"Skill '{skill.name}' has {len(skill.keywords)} keywords (min 2)"

    def test_every_skill_has_security_header(self):
        """Every skill prompt should start with security warnings."""
        skills = load_skills()
        for skill in skills.values():
            assert "security" in skill.system_prompt.lower()[:500], f"Skill '{skill.name}' missing security header"
