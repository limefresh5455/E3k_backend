"""Load and validate supplier folder/email mappings from Excel."""

import logging
import re
from pathlib import Path

import pandas as pd


EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _has_header(first_folder: str, first_email: str) -> bool:
    folder = first_folder.strip().casefold()
    email = first_email.strip().casefold()
    return "folder" in folder and "email" in email


def load_supplier_mapping(
    excel_path: Path,
    logger: logging.Logger,
) -> tuple[dict, dict]:
    """Return unambiguous exact-email and domain-to-folder mappings."""
    if not excel_path.exists():
        logger.error("Supplier Excel file not found: %s", excel_path)
        return {}, {}

    try:
        frame = pd.read_excel(
            excel_path,
            header=None,
            usecols=[0, 1],
            dtype=str,
            engine="openpyxl",
        ).fillna("")
        if frame.empty:
            logger.error("Supplier Excel file is empty: %s", excel_path)
            return {}, {}
        if _has_header(str(frame.iloc[0, 0]), str(frame.iloc[0, 1])):
            frame = frame.iloc[1:]
        frame.columns = ["folder", "email"]

        email_candidates: dict[str, set[str]] = {}
        canonical_folders: dict[str, str] = {}
        folder_variants: set[tuple[str, str]] = set()
        invalid_email_count = 0
        blank_email_count = 0
        blank_folder_count = 0

        for row in frame.itertuples(index=False):
            folder = str(row.folder).strip()
            email = str(row.email).strip().lower()
            if not folder:
                blank_folder_count += 1
                continue
            if not email:
                blank_email_count += 1
                continue
            if not EMAIL_PATTERN.fullmatch(email):
                invalid_email_count += 1
                continue

            # pCloud folder names are compared case-insensitively. Treat Excel
            # spellings that differ only by capitalization as the same supplier
            # and preserve the first spelling as the canonical folder name.
            folder_identity = folder.casefold()
            canonical_folder = canonical_folders.setdefault(folder_identity, folder)
            if folder != canonical_folder:
                folder_variants.add((canonical_folder, folder))
            email_candidates.setdefault(email, set()).add(canonical_folder)

        exact_map = {
            email: next(iter(folders))
            for email, folders in email_candidates.items()
            if len(folders) == 1
        }
        ambiguous_emails = sorted(
            email for email, folders in email_candidates.items() if len(folders) > 1
        )

        domain_suppliers: dict[str, set[str]] = {}
        for email, folder in exact_map.items():
            domain_suppliers.setdefault(email.rsplit("@", 1)[1], set()).add(folder)
        domain_map = {
            domain: next(iter(folders))
            for domain, folders in domain_suppliers.items()
            if len(folders) == 1
        }
        ambiguous_domains = sorted(
            domain for domain, folders in domain_suppliers.items() if len(folders) > 1
        )

        logger.info(
            "Loaded %d unambiguous supplier email mappings from '%s'",
            len(exact_map),
            excel_path.name,
        )
        logger.info("Loaded %d unambiguous domain fallback mappings", len(domain_map))
        if blank_email_count:
            logger.info("Ignored %d supplier row(s) without an email address", blank_email_count)
        if blank_folder_count:
            logger.warning("Ignored %d row(s) without a folder name", blank_folder_count)
        if invalid_email_count:
            logger.warning("Ignored %d row(s) containing invalid email values", invalid_email_count)
        if folder_variants:
            logger.info(
                "Normalized %d capitalization-only supplier folder variant(s): %s",
                len(folder_variants),
                ", ".join(
                    f"'{variant}' -> '{canonical}'"
                    for canonical, variant in sorted(folder_variants)
                ),
            )
        if ambiguous_emails:
            logger.warning(
                "Disabled %d email mapping(s) assigned to multiple folders: %s",
                len(ambiguous_emails),
                ", ".join(ambiguous_emails),
            )
        if ambiguous_domains:
            logger.warning(
                "Disabled fallback matching for %d ambiguous domain(s): %s",
                len(ambiguous_domains),
                ", ".join(ambiguous_domains),
            )
        return exact_map, domain_map
    except Exception as exc:
        logger.exception("Failed to load supplier mapping: %s", exc)
        return {}, {}
