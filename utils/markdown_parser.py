# Copyright (c) 2025-2026 Telomere LLC. All rights reserved.
# Proprietary and confidential. See LICENSE file in the project root.

"""
Markdown parser for code proposals.

Replaces strict JSON parsing with flexible markdown format to improve
LLM success rate from 40-70% to 90%+.
"""

from __future__ import annotations

import re
from typing import Optional

from schemas.code_proposal import CodeProposal, FileDiff, ChangeType


def parse_markdown_code_proposal(
    markdown_text: str,
    ticket_id: str,
) -> Optional[CodeProposal]:
    """
    Parse markdown-formatted code proposal into CodeProposal object.

    Expected format:
    ```markdown
    # Summary
    Brief summary of changes

    # Confidence
    0.85

    ## File: path/to/file.ts
    Type: modify
    Confidence: 0.9

    ```diff
    - old code
    + new code
    ```

    Rationale: Explanation here

    # Dependencies
    - package-name@version

    # Caveats
    - Warning or assumption
    ```

    Returns None if parsing fails completely.
    """
    if not markdown_text or not markdown_text.strip():
        return None

    try:
        # Extract summary
        summary = _extract_summary(markdown_text)
        if not summary:
            summary = "No summary provided"

        # Extract overall confidence
        confidence = _extract_confidence(markdown_text)

        # Extract file changes
        file_changes = _extract_file_changes(markdown_text)

        # Extract dependencies
        dependencies = _extract_dependencies(markdown_text)

        # Extract configuration changes
        config_changes = _extract_configuration_changes(markdown_text)

        # Extract migration scripts
        migrations = _extract_migration_scripts(markdown_text)

        # Extract caveats
        caveats = _extract_caveats(markdown_text)

        return CodeProposal(
            ticket_id=ticket_id,
            summary=summary,
            file_changes=file_changes,
            new_dependencies=dependencies,
            configuration_changes=config_changes,
            migration_scripts=migrations,
            confidence_score=confidence,
            caveats=caveats,
            raw_llm_response=markdown_text,
        )

    except Exception as e:
        # Log but don't crash - return None to signal parse failure
        print(f"Markdown parse error: {e}")
        return None


def _extract_summary(text: str) -> str:
    """Extract summary section."""
    # Look for "# Summary" or "## Summary" followed by content
    pattern = r'#+ *Summary\s*\n(.*?)(?=\n#+|\Z)'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()

    # Fallback: first paragraph
    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#') and not line.startswith('```'):
            return line

    return "Code proposal"


def _extract_confidence(text: str) -> float:
    """Extract overall confidence score."""
    # Look for "# Confidence" or "Confidence:" followed by a number
    patterns = [
        r'#+ *Confidence\s*[:\n]\s*([0-9]*\.?[0-9]+)',
        r'Confidence\s*[:\n]\s*([0-9]*\.?[0-9]+)',
        r'confidence_score\s*[:\n]\s*([0-9]*\.?[0-9]+)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                score = float(match.group(1))
                return max(0.0, min(1.0, score))  # Clamp to [0, 1]
            except ValueError:
                continue

    # Default: medium confidence
    return 0.75


def _extract_file_changes(text: str) -> list[FileDiff]:
    """Extract file change sections."""
    file_changes = []

    # Pattern: ## File: path/to/file.ext
    # Captures everything until next file or end
    pattern = r'#+ *File:\s*([^\n]+)\s*\n(.*?)(?=\n#+ *File:|\Z)'
    matches = re.finditer(pattern, text, re.IGNORECASE | re.DOTALL)

    for match in matches:
        file_path = match.group(1).strip()
        content_block = match.group(2).strip()

        # Extract change type
        change_type = _extract_change_type(content_block)

        # Extract diff/code content
        proposed_content, is_diff = _extract_code_content(content_block)

        # Extract rationale
        rationale = _extract_rationale(content_block)

        # Extract original content snippet (optional)
        original_snippet = _extract_original_snippet(content_block)

        if proposed_content:  # Only add if we found actual code
            file_changes.append(
                FileDiff(
                    file_path=file_path,
                    change_type=change_type,
                    original_content_snippet=original_snippet,
                    proposed_content=proposed_content,
                    is_diff_format=is_diff,
                    rationale=rationale or "No rationale provided",
                )
            )

    return file_changes


def _extract_change_type(text: str) -> ChangeType:
    """Extract change type from file block."""
    text_lower = text.lower()

    if re.search(r'\btype\s*:\s*create\b', text_lower):
        return ChangeType.CREATE
    elif re.search(r'\btype\s*:\s*delete\b', text_lower):
        return ChangeType.DELETE
    elif re.search(r'\btype\s*:\s*rename\b', text_lower):
        return ChangeType.RENAME
    elif re.search(r'\btype\s*:\s*modify\b', text_lower):
        return ChangeType.MODIFY

    # Check for keywords in text
    if 'new file' in text_lower or 'create' in text_lower:
        return ChangeType.CREATE
    elif 'delete' in text_lower or 'remove' in text_lower:
        return ChangeType.DELETE
    elif 'rename' in text_lower:
        return ChangeType.RENAME

    # Default: modify (most common)
    return ChangeType.MODIFY


def _extract_code_content(text: str) -> tuple[str, bool]:
    """
    Extract code content from file block.
    Returns (content, is_diff_format).
    """
    # Look for code blocks (```diff, ```typescript, etc.)
    code_block_pattern = r'```(?:diff|typescript|javascript|python|tsx|jsx|ts|js|py)?\s*\n(.*?)```'
    matches = list(re.finditer(code_block_pattern, text, re.DOTALL))

    if matches:
        # Use the first (usually largest) code block
        content = matches[0].group(1).strip()

        # Check if it's diff format
        is_diff = content.startswith('---') or content.startswith('@@') or ('-' in content and '+' in content)

        return content, is_diff

    # Fallback: look for indented code blocks (4+ spaces or tabs)
    lines = text.split('\n')
    code_lines = []
    in_code_block = False

    for line in lines:
        if line.startswith('    ') or line.startswith('\t'):
            in_code_block = True
            code_lines.append(line.lstrip())
        elif in_code_block and line.strip():
            break  # End of code block

    if code_lines:
        content = '\n'.join(code_lines)
        is_diff = content.startswith('---') or content.startswith('@@') or ('-' in content and '+' in content)
        return content, is_diff

    # Last resort: return entire block as code
    return text, False


def _extract_rationale(text: str) -> Optional[str]:
    """Extract rationale/reason for the change."""
    patterns = [
        r'Rationale\s*[:\n]\s*(.*?)(?=\n#+|\n\n|\Z)',
        r'Reason\s*[:\n]\s*(.*?)(?=\n#+|\n\n|\Z)',
        r'Why\s*[:\n]\s*(.*?)(?=\n#+|\n\n|\Z)',
        r'Explanation\s*[:\n]\s*(.*?)(?=\n#+|\n\n|\Z)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            rationale = match.group(1).strip()
            if rationale:
                return rationale

    return None


def _extract_original_snippet(text: str) -> Optional[str]:
    """Extract original content snippet (optional)."""
    pattern = r'Original\s*[:\n]\s*```.*?\n(.*?)```'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _extract_dependencies(text: str) -> list[str]:
    """Extract new dependencies."""
    deps = []

    # Look for "# Dependencies" or "# New Dependencies" section
    pattern = r'#+ *(?:New )?Dependencies\s*\n(.*?)(?=\n#+|\Z)'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)

    if match:
        content = match.group(1)
        # Extract bullet points or lines with package names
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('-') or line.startswith('*'):
                line = line[1:].strip()
            if line and not line.startswith('#'):
                deps.append(line)

    return deps


def _extract_configuration_changes(text: str) -> list[str]:
    """Extract configuration changes."""
    changes = []

    pattern = r'#+ *Configuration\s*(?:Changes)?\s*\n(.*?)(?=\n#+|\Z)'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)

    if match:
        content = match.group(1)
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('-') or line.startswith('*'):
                line = line[1:].strip()
            if line and not line.startswith('#'):
                changes.append(line)

    return changes


def _extract_migration_scripts(text: str) -> list[str]:
    """Extract migration scripts."""
    scripts = []

    pattern = r'#+ *Migrations?\s*(?:Scripts?)?\s*\n(.*?)(?=\n#+|\Z)'
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)

    if match:
        content = match.group(1)
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('-') or line.startswith('*'):
                line = line[1:].strip()
            if line and not line.startswith('#'):
                scripts.append(line)

    return scripts


def _extract_caveats(text: str) -> list[str]:
    """Extract caveats/warnings."""
    caveats = []

    patterns = [
        r'#+ *Caveats\s*\n(.*?)(?=\n#+|\Z)',
        r'#+ *Assumptions\s*\n(.*?)(?=\n#+|\Z)',
        r'#+ *Warnings?\s*\n(.*?)(?=\n#+|\Z)',
        r'#+ *Notes?\s*\n(.*?)(?=\n#+|\Z)',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if match:
            content = match.group(1)
            for line in content.split('\n'):
                line = line.strip()
                if line.startswith('-') or line.startswith('*'):
                    line = line[1:].strip()
                if line and not line.startswith('#'):
                    caveats.append(line)

    return caveats
