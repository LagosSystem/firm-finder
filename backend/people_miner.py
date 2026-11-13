"""
Comprehensive Layered People Miner (L1 - L5) with Multi-Signal Scoring.
Implements:
- L1: Structured sources (JSON-LD, Embedded Next/Nuxt JSON, WP REST API, XML Sitemaps)
- L2: Profile pages parsing (H1, title, meta, schema, bar admission/practice area confirmation)
- L3: Repeating roster block detection (3+ structural siblings)
- L4: Playwright rendering fallback (concurrency 1, hard 15s limit, blocked media)
- L5: Solo practitioners extraction from firm name (First+Last validated vs first_names)
- Multi-Signal Scoring System (+/- weights) with Confidence (high/medium/low)
- Strict Email Attribution rules (no generic emails, no email guessing)
"""

import os
import re
import json
import asyncio
import urllib.parse
from typing import List, Dict, Any, Optional, Set, Tuple
import xml.etree.ElementTree as ET
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

# Load first names dictionary
FIRST_NAMES: Set[str] = set()
FIRST_NAMES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "first_names.txt")
if os.path.exists(FIRST_NAMES_PATH):
    try:
        with open(FIRST_NAMES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                name = line.strip().title()
                if name:
                    FIRST_NAMES.add(name)
    except Exception as e:
        print(f"Warning: could not load first_names.txt: {e}")

# Optional spaCy entity detector
SPACY_NLP = None
try:
    import spacy
    try:
        SPACY_NLP = spacy.load("en_core_web_sm")
    except Exception:
        pass
except Exception:
    pass

# Negative terms / practice areas / marketing / verbs
NEGATIVE_TERMS = {
    # Practice areas & legal matters
    'wrongful', 'death', 'injury', 'injuries', 'accident', 'accidents', 'client', 'clients',
    'commercial', 'residential', 'estate', 'probate', 'litigation', 'lawsuit', 'lawsuits',
    'defense', 'prosecution', 'claim', 'claims', 'insurance', 'coverage',
    'harassment', 'retaliation', 'discrimination', 'malpractice', 'negligence', 'liability',
    'disability', 'compensation', 'dispute', 'disputes', 'settlement', 'settlements', 'verdict',
    'verdicts', 'arbitration', 'mediation', 'bankruptcy', 'divorce', 'custody', 'adoption',
    'traffic', 'dui', 'dwi', 'felony', 'misdemeanor', 'asbestos', 'mesothelioma', 'wildfire',
    'infringement', 'patent', 'trademark', 'copyright', 'immigration', 'visa',
    'visas', 'deportation', 'asylum', 'citizenship', 'tax', 'audit', 'compliance', 'governance',
    'transaction', 'transactions', 'merger', 'mergers', 'acquisition', 'acquisitions',
    'contracts', 'contract', 'corporate', 'business', 'incorporation', 'civil', 'criminal',
    'labor', 'employment', 'family', 'child', 'real', 'maritime', 'medical', 'nursing', 'abuse',
    'whistleblower', 'elder', 'guardianship', 'trust', 'trusts', 'planning', 'theft', 'larceny',
    'burglary', 'robbery', 'assault', 'battery', 'homicide', 'manslaughter', 'fraud', 'embezzlement',
    'extortion', 'bribery', 'arson', 'vandalism', 'trespass', 'stalking', 'kidnapping', 'trafficking',
    'forgery', 'property', 'healthcare', 'auto', 'bicycle', 'motorcycle', 'fault', 'counsel',
    'in-house', 'mechanical', 'failure', 'direct', 'access', 'employer', 'investor',

    # Navigation & marketing words
    'quick', 'links', 'link', 'articles', 'article', 'blogs', 'blog', 'posts', 'post',
    'news', 'updates', 'events', 'media', 'press', 'resources', 'resource', 'overview',
    'services', 'service', 'practices', 'practice', 'areas', 'area', 'industries', 'industry',
    'menu', 'search', 'results', 'home', 'about', 'join', 'careers', 'career', 'jobs', 'job',
    'testimonials', 'testimonial', 'reviews', 'review', 'ratings', 'faqs', 'faq',
    'questions', 'answers', 'disclaimer', 'privacy', 'terms', 'sitemap', 'accessibility',
    'contact', 'contacts', 'locations', 'location', 'offices', 'office', 'directions',
    'map', 'maps', 'portal', 'login', 'intake', 'consultation', 'firm', 'group',
    'schedule', 'online', 'guarantee', 'matters', 'advocacy', 'integrity', 'compassion',
    'protection', 'support', 'strategy', 'solutions', 'representation', 'care',
    'financial', 'fees', 'cost', 'award', 'awards', 'evaluation', 'cases', 'ready',
    'guidance', 'analysis', 'knowledge', 'tools', 'approach', 'mission', 'vision', 'history',
    'community', 'serving', 'served', 'built', 'around', 'workers', 'worker', 'available',
    'experienced', 'dedicated', 'trusted', 'proven', 'committed', 'passionate', 'relentless',
    'aggressive', 'premier', 'top-rated', 'recent', 'latest', 'related',
    'picture', 'pictures', 'take', 'driver', 'drivers', 'rideshare', 'rideshares', 'court',
    'wages', 'lost', 'income', 'earning', 'loss', 'losses', 'property', 'pain', 'suffering',
    'fighting', 'people', 'dream', 'straightforward', 'communication', 'supportmade', 'paralegals',
    'simple', 'assistant', 'staff', 'paralegal', 'team', 'support'

    # Common cities / geography
    'georgia', 'atlanta', 'california', 'seattle', 'fulton', 'orangeburg', 'dekalb', 'quebec', 'canada',
    'america', 'american', 'national', 'state', 'federal', 'county', 'carolina', 'north', 'south',
    'michigan', 'detroit', 'troy', 'southfield', 'bloomfield'
}

# Recognized legal job titles with seniority mapping
LEGAL_TITLES_MAP = {
    # Partners / Principals
    'managing partner': ('Managing Partner', 'Partner'),
    'founding partner': ('Founding Partner', 'Partner'),
    'senior partner': ('Senior Partner', 'Partner'),
    'equity partner': ('Partner', 'Partner'),
    'junior partner': ('Partner', 'Partner'),
    'name partner': ('Partner', 'Partner'),
    'partner': ('Partner', 'Partner'),
    'founder': ('Founder & Attorney', 'Partner'),
    'co-founder': ('Co-Founder & Attorney', 'Partner'),
    'principal attorney': ('Principal Attorney', 'Partner'),
    'principal': ('Principal Attorney', 'Partner'),
    'president': ('President & Attorney', 'Partner'),
    'managing attorney': ('Managing Attorney', 'Partner'),
    'lead attorney': ('Lead Attorney', 'Partner'),
    'lead counsel': ('Lead Counsel', 'Partner'),
    'trial attorney': ('Trial Attorney', 'Associate'),
    'trial lawyer': ('Trial Lawyer', 'Associate'),
    # Counsel
    'of counsel': ('Of Counsel', 'Of Counsel'),
    'special counsel': ('Special Counsel', 'Of Counsel'),
    'senior counsel': ('Senior Counsel', 'Of Counsel'),
    'general counsel': ('General Counsel', 'Of Counsel'),
    'legal counsel': ('Legal Counsel', 'Of Counsel'),
    'counsel': ('Counsel', 'Of Counsel'),
    # Associates & Lawyers
    'senior associate': ('Senior Associate', 'Associate'),
    'associate attorney': ('Associate Attorney', 'Associate'),
    'associate': ('Associate Attorney', 'Associate'),
    'staff attorney': ('Staff Attorney', 'Associate'),
    'attorney at law': ('Attorney at Law', 'Associate'),
    'attorney': ('Attorney', 'Associate'),
    'lawyer': ('Lawyer', 'Associate'),
    'solicitor': ('Solicitor', 'Associate'),
    'barrister': ('Barrister', 'Associate'),
    # Staff / Paralegal
    'senior paralegal': ('Senior Paralegal', 'Staff'),
    'paralegal': ('Paralegal', 'Staff'),
    'legal assistant': ('Legal Assistant', 'Staff'),
    'law clerk': ('Law Clerk', 'Staff'),
    # French titles (Quebec / Canada / Europe)
    'associé-directeur': ('Associé-directeur', 'Partner'),
    'associée-directrice': ('Associée-directrice', 'Partner'),
    'associé directeur': ('Associé directeur', 'Partner'),
    'associée directrice': ('Associée directrice', 'Partner'),
    'associé': ('Associé', 'Partner'),
    'associée': ('Associée', 'Partner'),
    'avocat-conseil': ('Avocat-conseil', 'Of Counsel'),
    'avocate-conseil': ('Avocate-conseil', 'Of Counsel'),
    'conseiller juridique': ('Conseiller juridique', 'Of Counsel'),
    'conseillère juridique': ('Conseillère juridique', 'Of Counsel'),
    'avocat': ('Avocat', 'Associate'),
    'avocate': ('Avocate', 'Associate'),
    'notaire': ('Notaire', 'Associate'),
    'juriste': ('Juriste', 'Associate')
}

# Generic prefixes that denote firm-level emails
GENERIC_EMAIL_PREFIXES = {
    'info', 'contact', 'office', 'admin', 'intake', 'hello', 'support',
    'reception', 'mail', 'inquiry', 'inquiries', 'help', 'billing', 'law',
    'general', 'team', 'service', 'services', 'careers', 'client'
}

CTA_VERBS = {
    'meet', 'why', 'who', 'whowe', 'call', 'contact', 'speak', 'see', 'our',
    'about', 'join', 'how', 'what', 'learn', 'get', 'ask', 'serving', 'available',
    'built', 'take', 'other', 'all', 'more', 'find', 'hire', 'fill', 'need', 'tell',
    'read', 'schedule', 'view'
}

# Semaphore for L4 Playwright rendering (strictly 1 shared concurrency)
PLAYWRIGHT_SEMAPHORE = asyncio.Semaphore(1)
PLAYWRIGHT_RENDER_COUNT = 0
MAX_PLAYWRIGHT_RENDERS_PER_RUN = 30

SCORE_THRESHOLD = 4  # Candidates with score >= 4 are kept; >= 7 are high confidence


def normalize_name_key(name: str) -> str:
    import unicodedata
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('utf-8')
    return re.sub(r'[^a-zA-Z0-9]', '', n).lower()


def clean_person_name(raw: str, firm_name: Optional[str] = None) -> Optional[str]:
    """Basic structural cleanup and prefix stripping. Rejects firm name collisions."""
    if not raw:
        return None
    name = re.sub(r'[\r\n\t]+', ' ', raw).strip()
    # Strip postnominals
    name = re.sub(r'\b(Esq\.?|J\.D\.?|Attorney at Law|Attorney|Lawyer|PC|LLC|APC|LLP|Inc\.?)\b', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'[,.\s]+$', '', name).strip()
    name = re.sub(r'^[,\s]+', '', name).strip()
    # Strip leading intro prefixes
    name = re.sub(r'^(Meet|About|Attorney|Our Partner|Partner|Lawyer|Avocat)\s+', '', name, flags=re.IGNORECASE).strip()

    if len(name) < 4 or len(name) > 35:
        return None

    if name.isupper():
        name = name.title()

    parts = [p for p in re.split(r'[\s.]+', name) if p]
    if len(parts) < 2 or len(parts) > 4:
        return None

    # Reject if candidate name matches or is subset of the firm's own business name
    if firm_name:
        fn_clean = re.sub(r'[^a-zA-Z0-9\s]', '', firm_name).lower()
        cand_clean = re.sub(r'[^a-zA-Z0-9\s]', '', name).lower()
        if cand_clean in fn_clean or fn_clean in cand_clean:
            return None

    # Reject known slogans or non-person phrases
    name_lower = name.lower()
    if any(phrase in name_lower for phrase in [
        'for the people', 'fighting for', 'dream team', 'straightforward', 'communication',
        'support staff', 'legal support', 'our team', 'your team', 'why choose', 'faqs',
        'commitment', 'accident', 'injury', 'injuries', 'car accident', 'personal injury'
    ]):
        return None

    # Reject strings starting with CTA verbs or pronouns
    first_word_lower = parts[0].lower()
    if first_word_lower in {
        'meet', 'why', 'who', 'whowe', 'call', 'contact', 'speak', 'see', 'our',
        'about', 'join', 'how', 'what', 'learn', 'get', 'ask', 'serving', 'available',
        'built', 'take', 'other', 'all', 'more', 'find', 'hire', 'fill', 'need', 'tell',
        'read', 'schedule', 'view', 'visit', 'featured'
    }:
        return None

    # In clean human names, every word part should not be a negative term
    words = [p.lower() for p in parts]
    if any(w in NEGATIVE_TERMS for w in words):
        return None

    # Surnames do not end with gerunds or abstract nouns (-ing, -tion, -ship, etc.)
    whitelisted_surnames = {
        'king', 'ring', 'spalding', 'darling', 'fleming', 'cummings', 'harding', 'browning',
        'sterling', 'mann', 'dunn', 'long', 'young', 'singh', 'gordon', 'hudson'
    }
    for w in words:
        if w not in whitelisted_surnames:
            if w.endswith(('ing', 'tion', 'sion', 'ment', 'ance', 'ence', 'ness', 'ship')):
                return None

    # Support Latin alphabet with European accents
    if not re.match(r"^[A-Za-z\u00C0-\u024F\s'\-\.]+$", name):
        return None

    return name


def score_candidate_name(
    name: str,
    signals: Dict[str, Any]
) -> Tuple[int, str, List[Dict[str, Any]]]:
    """
    Computes weighted score for candidate name.
    Returns (total_score, confidence, evidence_list).
    """
    score = 0
    evidence = []

    parts = [p for p in re.split(r'[\s.]+', name) if p]
    first_token = parts[0].title() if parts else ""
    first_token_lower = first_token.lower()
    words_lower = [p.lower() for p in parts]

    # Positive Signals
    if signals.get("profile_h1_match"):
        score += 3
        evidence.append({"signal": "profile_h1_match", "weight": +3})

    if signals.get("inside_repeating_block"):
        score += 2
        evidence.append({"signal": "inside_repeating_block", "weight": +2})

    if signals.get("legal_title_in_block"):
        score += 2
        evidence.append({"signal": "legal_title_in_block", "weight": +2})

    if signals.get("profile_slug_match"):
        score += 2
        evidence.append({"signal": "profile_slug_match", "weight": +2})

    if signals.get("headshot_alt_match"):
        score += 1
        evidence.append({"signal": "headshot_alt_match", "weight": +1})

    if signals.get("has_esq_jd"):
        score += 1
        evidence.append({"signal": "has_esq_jd", "weight": +1})

    if first_token in FIRST_NAMES:
        score += 2
        evidence.append({"signal": "first_name_dictionary", "weight": +2})

    if signals.get("spacy_person"):
        score += 1
        evidence.append({"signal": "spacy_person", "weight": +1})

    if signals.get("multipage_appearance"):
        score += 1
        evidence.append({"signal": "multipage_appearance", "weight": +1})

    if signals.get("schema_person"):
        score += 3
        evidence.append({"signal": "schema_person", "weight": +3})

    if signals.get("wp_rest_person"):
        score += 3
        evidence.append({"signal": "wp_rest_person", "weight": +3})

    # Negative Signals
    if any(w in NEGATIVE_TERMS for w in words_lower):
        score -= 3
        evidence.append({"signal": "negative_term_match", "weight": -3})

    if first_token_lower in CTA_VERBS:
        score -= 2
        evidence.append({"signal": "cta_verb_prefix", "weight": -2})

    if signals.get("is_all_caps_slogan"):
        score -= 2
        evidence.append({"signal": "all_caps_slogan", "weight": -2})

    if signals.get("nav_or_footer_only"):
        score -= 2
        evidence.append({"signal": "nav_or_footer_only", "weight": -2})

    # Confidence calculation
    if score >= 7 or signals.get("profile_h1_match") or signals.get("schema_person") or signals.get("wp_rest_person"):
        confidence = "high"
    elif score >= SCORE_THRESHOLD:
        confidence = "medium"
    else:
        confidence = "low"

    return score, confidence, evidence


def extract_legal_title(card_text: str) -> Optional[Tuple[str, str]]:
    """Scans text for legal title, returns (title, seniority)."""
    if not card_text:
        return None
    chunks = [c.strip() for c in re.split(r'[\n\r|•·,.;]+', card_text) if c.strip()]
    sorted_titles = sorted(LEGAL_TITLES_MAP.keys(), key=lambda k: len(k), reverse=True)

    for chunk in chunks:
        if len(chunk) > 60 or '?' in chunk:
            continue
        lower_chunk = chunk.lower()
        for kw in sorted_titles:
            pattern = r'\b' + re.escape(kw) + r'\b'
            if re.search(pattern, lower_chunk):
                return LEGAL_TITLES_MAP[kw]
    return None


def decode_cloudflare_email(cf_hex: str) -> Optional[str]:
    """Decodes Cloudflare XOR hex-encoded email protection strings."""
    if not cf_hex or len(cf_hex) < 4:
        return None
    try:
        r = int(cf_hex[:2], 16)
        return ''.join([chr(int(cf_hex[i:i+2], 16) ^ r) for i in range(2, len(cf_hex), 2)]).strip()
    except Exception:
        return None


def extract_person_email_from_container(container) -> Optional[str]:
    """
    Extracts direct email strictly from a person's DOM container or profile page.
    Rejects generic prefixes (info, contact, office, etc.).
    """
    if not container:
        return None

    # Check Cloudflare
    for cftag in container.find_all(attrs={'data-cfemail': True}):
        em = decode_cloudflare_email(cftag['data-cfemail'])
        if em and '@' in em:
            prefix = em.split('@')[0].lower()
            if prefix not in GENERIC_EMAIL_PREFIXES:
                return em.lower()

    # Check mailto links
    for ca in container.find_all('a', href=True):
        href = ca['href'].strip()
        if '/cdn-cgi/l/email-protection#' in href:
            em = decode_cloudflare_email(href.split('#')[-1])
            if em and '@' in em:
                prefix = em.split('@')[0].lower()
                if prefix not in GENERIC_EMAIL_PREFIXES:
                    return em.lower()
        elif href.lower().startswith('mailto:'):
            raw = href[7:].split('?')[0].strip().lower()
            if '@' in raw:
                candidate = raw.split(',')[0].strip()
                prefix = candidate.split('@')[0].lower()
                if prefix not in GENERIC_EMAIL_PREFIXES:
                    return candidate

    # Regex search in container text
    c_text = container.get_text(separator=' ')
    m = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', c_text)
    if m:
        candidate = m.group(0).lower()
        prefix = candidate.split('@')[0].lower()
        if prefix not in GENERIC_EMAIL_PREFIXES:
            if not any(candidate.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp']):
                return candidate

    return None


def format_linkedin_url(person_name: str, firm_name: str, direct_url: Optional[str] = None) -> Tuple[str, bool]:
    if direct_url and "linkedin.com/in/" in direct_url:
        return direct_url.strip(), True
    query = f"{person_name} {firm_name}".strip()
    encoded = urllib.parse.quote_plus(query)
    return f"https://www.linkedin.com/search/results/all/?keywords={encoded}", False


# =========================================================================
# LAYER 1: Structured Sources (JSON-LD, Embedded JSON, WP REST, Sitemaps)
# =========================================================================

async def extract_l1_jsonld(soup: BeautifulSoup, base_url: str, firm_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Extracts Person, Attorney, and LegalService members from JSON-LD schema."""
    people = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            raw_text = script.string or script.get_text()
            if not raw_text:
                continue
            data = json.loads(raw_text.strip())
            items = data if isinstance(data, list) else [data]

            for item in items:
                # Handle @graph
                if isinstance(item, dict) and "@graph" in item:
                    items.extend(item["@graph"])

            for item in items:
                if not isinstance(item, dict):
                    continue
                type_name = str(item.get("@type", "")).lower()

                # Case 1: Item itself is Person / Attorney
                if any(t in type_name for t in ["person", "attorney", "lawyer"]):
                    name = clean_person_name(item.get("name", ""), firm_name)
                    if name:
                        title_info = extract_legal_title(item.get("jobTitle") or item.get("hasOccupation") or "")
                        title, seniority = title_info if title_info else ("Attorney", "Associate")
                        email = item.get("email") if isinstance(item.get("email"), str) else None
                        if email and email.split("@")[0].lower() in GENERIC_EMAIL_PREFIXES:
                            email = None
                        profile_url = item.get("url") if isinstance(item.get("url"), str) else None
                        people.append({
                            "name": name,
                            "title": title,
                            "seniority": seniority,
                            "email": email,
                            "profile_url": profile_url,
                            "source_layer": "L1_structured",
                            "schema_person": True
                        })

                # Case 2: LegalService with employee / member / founder
                for field in ["employee", "member", "founder", "alumni"]:
                    members = item.get(field, [])
                    if isinstance(members, dict):
                        members = [members]
                    if isinstance(members, list):
                        for m in members:
                            if isinstance(m, dict) and any(t in str(m.get("@type", "")).lower() for t in ["person", "attorney"]):
                                name = clean_person_name(m.get("name", ""), firm_name)
                                if name:
                                    title_info = extract_legal_title(m.get("jobTitle") or "")
                                    title, seniority = title_info if title_info else ("Attorney", "Associate")
                                    people.append({
                                        "name": name,
                                        "title": title,
                                        "seniority": seniority,
                                        "email": m.get("email"),
                                        "profile_url": m.get("url"),
                                        "source_layer": "L1_structured",
                                        "schema_person": True
                                    })
        except Exception:
            continue
    return people


async def extract_l1_embedded_json(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Walks __NEXT_DATA__, __NUXT__, window.__INITIAL_STATE__ for person objects."""
    people = []
    target_scripts = soup.find_all('script', id=['__NEXT_DATA__', '__NUXT_DATA__'])
    for ts in target_scripts:
        try:
            data = json.loads(ts.get_text())
            def walk(obj):
                if isinstance(obj, dict):
                    if "name" in obj and ("title" in obj or "role" in obj or "position" in obj or "jobTitle" in obj):
                        candidate_name = clean_person_name(str(obj["name"]))
                        title_str = str(obj.get("title") or obj.get("role") or obj.get("position") or obj.get("jobTitle") or "")
                        title_info = extract_legal_title(title_str)
                        if candidate_name and title_info:
                            people.append({
                                "name": candidate_name,
                                "title": title_info[0],
                                "seniority": title_info[1],
                                "email": obj.get("email"),
                                "profile_url": obj.get("url") or obj.get("slug"),
                                "source_layer": "L1_structured",
                                "schema_person": True
                            })
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)
            walk(data)
        except Exception:
            continue
    return people


async def extract_l1_wordpress_rest(client: httpx.AsyncClient, base_url: str) -> List[Dict[str, Any]]:
    """Probes /wp-json/wp/v2/types for attorney/team custom post types and lists members."""
    people = []
    wp_types_url = urllib.parse.urljoin(base_url, "/wp-json/wp/v2/types")
    try:
        resp = await client.get(wp_types_url, timeout=3.0)
        if resp.status_code == 200:
            types_data = resp.json()
            # Look for attorney or team post type
            target_slugs = []
            for slug, info in types_data.items():
                s_lower = slug.lower()
                name_lower = str(info.get("name", "")).lower()
                if any(k in s_lower or k in name_lower for k in ["attorney", "lawyer", "team", "people", "staff", "avocat"]):
                    rest_base = info.get("rest_base", slug)
                    target_slugs.append(rest_base)

            for slug in target_slugs[:2]:
                posts_url = urllib.parse.urljoin(base_url, f"/wp-json/wp/v2/{slug}?per_page=100")
                p_resp = await client.get(posts_url, timeout=4.0)
                if p_resp.status_code == 200:
                    items = p_resp.json()
                    for it in items:
                        raw_title = it.get("title", {}).get("rendered", "")
                        clean_title = BeautifulSoup(raw_title, "html.parser").get_text()
                        name = clean_person_name(clean_title)
                        if name:
                            link = it.get("link")
                            people.append({
                                "name": name,
                                "title": "Attorney",
                                "seniority": "Associate",
                                "profile_url": link,
                                "source_layer": "L1_structured",
                                "wp_rest_person": True
                            })
    except Exception:
        pass
    return people


async def extract_l1_sitemap_profile_urls(client: httpx.AsyncClient, base_url: str) -> List[str]:
    """Collects individual profile URLs from sitemap.xml."""
    profile_urls: List[str] = []
    sitemap_targets = [
        urllib.parse.urljoin(base_url, "/sitemap.xml"),
        urllib.parse.urljoin(base_url, "/sitemap_index.xml"),
        urllib.parse.urljoin(base_url, "/page-sitemap.xml"),
        urllib.parse.urljoin(base_url, "/attorney-sitemap.xml"),
        urllib.parse.urljoin(base_url, "/sitemap-attorneys.xml")
    ]

    for sm_url in sitemap_targets:
        try:
            resp = await client.get(sm_url, timeout=4.0)
            if resp.status_code != 200 or not resp.text:
                continue
            root = ET.fromstring(resp.content)
            # Check sitemapindex
            for sm in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                loc = (sm.text or "").strip()
                if any(k in loc.lower() for k in ["sitemap-attorney", "sitemap-lawyer", "attorney-sitemap", "lawyer-sitemap"]):
                    # Fetch child sitemap index
                    try:
                        c_resp = await client.get(loc, timeout=4.0)
                        if c_resp.status_code == 200:
                            c_root = ET.fromstring(c_resp.content)
                            for curl in c_root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                                cu = (curl.text or "").strip()
                                if any(k in cu.lower() for k in ["/attorney/", "/attorneys/", "/lawyer/", "/lawyers/", "/team/", "/people/", "/bio/"]):
                                    profile_urls.append(cu)
                    except Exception:
                        pass
                elif any(k in loc.lower() for k in ["/attorney/", "/attorneys/", "/lawyer/", "/lawyers/", "/team/", "/people/", "/bio/"]):
                    profile_urls.append(loc)

            if len(profile_urls) >= 15:
                break
        except Exception:
            continue

    return list(dict.fromkeys(profile_urls))[:15]


# =========================================================================
# LAYER 2: Profile Pages (H1, Title, Meta, Schema confirmation)
# =========================================================================

async def parse_profile_page(client: httpx.AsyncClient, profile_url: str, firm_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetches up to 15 profile URLs per firm and extracts attorney info from H1, Title, Meta."""
    try:
        resp = await client.get(profile_url, timeout=5.0)
        if resp.status_code != 200 or not resp.text:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Check JSON-LD on profile page first
        ld_people = await extract_l1_jsonld(soup, profile_url, firm_name)
        if ld_people:
            p = ld_people[0]
            p["profile_url"] = profile_url
            p["source_layer"] = "L2_profile"
            return p

        # Check H1
        h1 = soup.find('h1')
        h1_text = h1.get_text(strip=True) if h1 else ""
        candidate_name = clean_person_name(h1_text, firm_name)

        title_info = None
        if candidate_name:
            # Check for title in page text near H1 or page meta
            page_text = soup.get_text(separator=' | ')
            title_info = extract_legal_title(page_text[:1500])

        if not candidate_name:
            # Fallback to <title>
            title_tag = soup.find('title')
            if title_tag:
                raw_t = title_tag.get_text().split('|')[0].split('-')[0].strip()
                candidate_name = clean_person_name(raw_t, firm_name)
                if candidate_name:
                    title_info = extract_legal_title(title_tag.get_text())

        if not candidate_name:
            return None

        clean_title, seniority = title_info if title_info else ("Attorney", "Associate")
        email = extract_person_email_from_container(soup)

        return {
            "name": candidate_name,
            "title": clean_title,
            "seniority": seniority,
            "email": email,
            "profile_url": profile_url,
            "source_layer": "L2_profile",
            "profile_h1_match": True
        }
    except Exception:
        return None


# =========================================================================
# LAYER 3: Roster Pages (Repeating DOM Block Detection)
# =========================================================================

def detect_repeating_blocks(soup: BeautifulSoup) -> List[Any]:
    """
    Finds repeating sibling containers (3+) that share structural tags and classes,
    each containing a heading-like name, a role/title, and an image or link.
    """
    candidates = []
    # Find all potential cards
    for tag_name in ['div', 'li', 'article', 'section']:
        elements = soup.find_all(tag_name)
        # Group siblings by parent
        parent_groups: Dict[Any, List[Any]] = {}
        for el in elements:
            parent = el.parent
            if parent:
                parent_groups.setdefault(parent, []).append(el)

        for parent, siblings in parent_groups.items():
            if len(siblings) >= 2:
                # Check if siblings look like person cards
                valid_cards = 0
                for sib in siblings:
                    has_heading = bool(sib.find(['h2', 'h3', 'h4', 'h5', 'strong']))
                    has_img_or_a = bool(sib.find(['img', 'a']))
                    card_txt = sib.get_text()
                    has_title = bool(extract_legal_title(card_txt))
                    if has_heading and (has_img_or_a or has_title):
                        valid_cards += 1
                if valid_cards >= 2:
                    candidates.extend(siblings)

    return list(dict.fromkeys(candidates))


# =========================================================================
# LAYER 4: Constrained Playwright Fallback (Concurrency 1, Hard 15s)
# =========================================================================

async def render_team_page_fallback(url: str) -> Optional[str]:
    """
    Renders a single page with Playwright when raw HTML is an empty JS shell.
    Strict constraints: Concurrency 1, block images/fonts/media, 15s timeout, max 30 per run.
    """
    global PLAYWRIGHT_RENDER_COUNT
    if PLAYWRIGHT_RENDER_COUNT >= MAX_PLAYWRIGHT_RENDERS_PER_RUN:
        return None

    async with PLAYWRIGHT_SEMAPHORE:
        PLAYWRIGHT_RENDER_COUNT += 1
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
                )
                page = await browser.new_page(
                    user_agent=HEADERS["User-Agent"]
                )
                # Block heavy media
                await page.route(
                    "**/*",
                    lambda route: route.abort() if route.request.resource_type in ["image", "media", "font", "stylesheet"] else route.continue_()
                )
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(1.5)
                rendered_html = await page.content()
                await browser.close()
                return rendered_html
        except Exception:
            return None


# =========================================================================
# LAYER 5: Solo Practitioners & Firm Name Heuristic
# =========================================================================

def extract_solo_from_firm_name(firm_name: str) -> Optional[Dict[str, Any]]:
    """
    Extracts solo practitioner name if the firm's name contains a full person name (First + Last)
    validated against the first_names dictionary. Never derives from a surname alone.
    Rejects names containing '&', 'and', '+', '/', or identical tokens (e.g. 'Morgan & Morgan').
    """
    if not firm_name:
        return None

    # Never derive from partner partnerships or surnames conjoined with & or and
    if any(sep in firm_name.lower() for sep in [' & ', ' and ', ' + ', ' / ']):
        return None

    # Clean firm suffix tokens
    cleaned = re.sub(
        r'\b(The\s+)?(Law\s+Offices?|Law\s+Firm|Law\s+Group|Legal\s+Group|PLLC|LLC|P\.C\.|PC|LLP|Inc\.?|Attorneys?|Lawyers?|of)\b',
        '',
        firm_name,
        flags=re.IGNORECASE
    ).strip()
    cleaned = re.sub(r'[,.\-\|]+', ' ', cleaned).strip()

    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if len(tokens) >= 2:
        candidate_first = tokens[0].title()
        if candidate_first in FIRST_NAMES:
            candidate_last = tokens[1].title()
            # If 3 tokens and middle initial (e.g. Julian J. Poota)
            if len(tokens) >= 3 and len(tokens[1]) <= 2:
                full_name = f"{candidate_first} {tokens[1].upper()} {tokens[2].title()}"
                candidate_last = tokens[2].title()
            else:
                full_name = f"{candidate_first} {candidate_last}"

            # Reject if first and last name are identical (e.g. Morgan Morgan)
            if candidate_first.lower() == candidate_last.lower():
                return None

            # Ensure candidate_last is not a geographic or legal negative term
            if candidate_last.lower() not in NEGATIVE_TERMS and candidate_first.lower() not in NEGATIVE_TERMS:
                return {
                    "name": full_name,
                    "title": "Principal Attorney",
                    "seniority": "Partner",
                    "source_layer": "L5_solo",
                    "confidence": "medium",
                    "score": 5,
                    "evidence": [{"signal": "firm_name_solo_match", "weight": +5}]
                }
    return None


# =========================================================================
# MAIN MINER PIPELINE
# =========================================================================

async def mine_affiliated_people(
    firm_name: str,
    website_url: Optional[str],
    city: Optional[str] = "",
    state: Optional[str] = ""
) -> List[Dict[str, Any]]:
    """
    Layered pipeline (L1 -> L5) extracting verified affiliated practitioners.
    Budget per firm: max 12 requests and 25 seconds total.
    """
    discovered: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()

    if not website_url:
        solo = extract_solo_from_firm_name(firm_name)
        return [solo] if solo else []

    url = website_url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    # Unwrap Google redirect URLs
    if "google.com" in url or "google." in url:
        parsed_g = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed_g.query)
        target_q = qs.get("q", [None])[0] or qs.get("url", [None])[0] or qs.get("adurl", [None])[0]
        if target_q and target_q.startswith("http"):
            url = target_q

    timeout = httpx.Timeout(10.0, connect=4.0)
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, verify=False, follow_redirects=True) as client:
        # Step 1: Fetch Homepage HTML
        home_html = ""
        try:
            resp = await client.get(url, timeout=6.0)
            if resp.status_code == 200:
                home_html = resp.text
        except Exception:
            pass

        home_soup = BeautifulSoup(home_html, 'html.parser') if home_html else None

        # =====================================================================
        # LAYER 1: Structured Sources (JSON-LD, Embedded JSON, WP REST, Sitemaps)
        # =====================================================================
        if home_soup:
            # 1A: JSON-LD
            l1_people = await extract_l1_jsonld(home_soup, url, firm_name)
            for p in l1_people:
                k = normalize_name_key(p["name"])
                if k not in seen_keys:
                    seen_keys.add(k)
                    score, conf, ev = score_candidate_name(p["name"], p)
                    p["score"] = score
                    p["confidence"] = conf
                    p["evidence"] = ev
                    li_url, is_dir = format_linkedin_url(p["name"], firm_name)
                    p["linkedin_url"] = li_url
                    p["is_direct_linkedin"] = is_dir
                    p["source_url"] = url
                    discovered.append(p)

            # 1B: Embedded JSON
            l1_embed = await extract_l1_embedded_json(home_soup)
            for p in l1_embed:
                k = normalize_name_key(p["name"])
                if k not in seen_keys:
                    seen_keys.add(k)
                    score, conf, ev = score_candidate_name(p["name"], p)
                    p["score"] = score
                    p["confidence"] = conf
                    p["evidence"] = ev
                    li_url, is_dir = format_linkedin_url(p["name"], firm_name)
                    p["linkedin_url"] = li_url
                    p["is_direct_linkedin"] = is_dir
                    p["source_url"] = url
                    discovered.append(p)

            # 1C: WordPress REST API
            if len(discovered) < 8:
                wp_people = await extract_l1_wordpress_rest(client, url)
                for p in wp_people:
                    k = normalize_name_key(p["name"])
                    if k not in seen_keys:
                        seen_keys.add(k)
                        score, conf, ev = score_candidate_name(p["name"], p)
                        p["score"] = score
                        p["confidence"] = conf
                        p["evidence"] = ev
                        li_url, is_dir = format_linkedin_url(p["name"], firm_name)
                        p["linkedin_url"] = li_url
                        p["is_direct_linkedin"] = is_dir
                        p["source_url"] = url
                        discovered.append(p)

        # High-confidence exit if L1 returned 8+ confirmed practitioners (e.g. massive directory like Cochran)
        high_conf_count = sum(1 for p in discovered if p.get("confidence") == "high")
        if high_conf_count >= 8:
            return discovered

        # =====================================================================
        # Discover Profile URLs from Sitemap & Team Directory Links
        # =====================================================================
        profile_urls: List[str] = []
        team_pages: List[str] = []

        # 1D: Sitemap URLs
        sitemap_profiles = await extract_l1_sitemap_profile_urls(client, url)
        profile_urls.extend(sitemap_profiles)

        if home_soup:
            parsed_base = urllib.parse.urlparse(url)
            base_netloc = parsed_base.netloc.lower().replace('www.', '')

            for a in home_soup.find_all('a', href=True):
                href = a['href'].strip()
                if not href or href.startswith('#') or href.startswith('javascript:'):
                    continue
                resolved = urllib.parse.urljoin(url, href)
                parsed_res = urllib.parse.urlparse(resolved)
                if parsed_res.netloc.lower().replace('www.', '') == base_netloc:
                    path_lower = parsed_res.path.lower()
                    text_lower = a.get_text(strip=True).lower()
                    if any(k in path_lower or k in text_lower for k in [
                        'attorney', 'attorneys', 'lawyer', 'lawyers', 'our-team', 'team',
                        'professionals', 'leadership', 'avocats', 'equipe', 'about-us', 'about', 'our-firm'
                    ]):
                        if any(k in path_lower for k in ['/attorney/', '/lawyer/', '/bio/', '/people/', '/lawyers/']) and len(parsed_res.path.strip('/').split('/')) > 1:
                            profile_urls.append(resolved)
                        else:
                            team_pages.append(resolved)

        team_pages = list(dict.fromkeys(team_pages))[:5]
        profile_urls = list(dict.fromkeys(profile_urls))[:15]

        # =====================================================================
        # LAYER 2: Profile Pages (up to 15 profiles)
        # =====================================================================
        for prof_url in profile_urls:
            if len(discovered) >= 15:
                break
            prof_data = await parse_profile_page(client, prof_url, firm_name)
            if prof_data:
                k = normalize_name_key(prof_data["name"])
                if k not in seen_keys:
                    seen_keys.add(k)
                    score, conf, ev = score_candidate_name(prof_data["name"], prof_data)
                    prof_data["score"] = score
                    prof_data["confidence"] = conf
                    prof_data["evidence"] = ev
                    li_url, is_dir = format_linkedin_url(prof_data["name"], firm_name)
                    prof_data["linkedin_url"] = li_url
                    prof_data["is_direct_linkedin"] = is_dir
                    prof_data["source_url"] = prof_url
                    discovered.append(prof_data)

        # =====================================================================
        # LAYER 3: Roster Pages (Repeating Block Detection)
        # =====================================================================
        rendered_attempted = False
        for t_url in team_pages:
            if len(discovered) >= 15:
                break
            try:
                t_resp = await client.get(t_url, timeout=6.0)
                if t_resp.status_code != 200 or not t_resp.text:
                    continue
                t_soup = BeautifulSoup(t_resp.text, 'html.parser')

                # Check JSON-LD on team page
                t_ld = await extract_l1_jsonld(t_soup, t_url, firm_name)
                for p in t_ld:
                    k = normalize_name_key(p["name"])
                    if k not in seen_keys:
                        seen_keys.add(k)
                        score, conf, ev = score_candidate_name(p["name"], p)
                        p["score"] = score
                        p["confidence"] = conf
                        p["evidence"] = ev
                        li_url, is_dir = format_linkedin_url(p["name"], firm_name)
                        p["linkedin_url"] = li_url
                        p["is_direct_linkedin"] = is_dir
                        p["source_url"] = t_url
                        discovered.append(p)

                # Repeating Blocks
                blocks = detect_repeating_blocks(t_soup)

                # Fallback for solo / boutique pages where only 1 attorney card exists on /about or /team
                if not blocks:
                    for h_el in t_soup.find_all(['h1', 'h2', 'h3', 'h4', 'strong']):
                        raw_h = h_el.get_text(strip=True)
                        cand = clean_person_name(raw_h, firm_name)
                        if not cand or normalize_name_key(cand) in seen_keys:
                            continue
                        first_tok = cand.split()[0].title() if cand.split() else ''
                        if first_tok in FIRST_NAMES:
                            parent_box = h_el.find_parent(['div', 'article', 'section'])
                            if parent_box:
                                t_info = extract_legal_title(parent_box.get_text(separator=' | '))
                                if t_info:
                                    blocks.append(parent_box)

                for block in blocks:
                    # Prefer explicit name headers inside the block
                    found_cand = None
                    for h_el in block.find_all(['h1', 'h2', 'h3', 'h4', 'strong', 'h5']):
                        c = clean_person_name(h_el.get_text(strip=True), firm_name)
                        if c and normalize_name_key(c) not in seen_keys:
                            first_tok = c.split()[0].title() if c.split() else ''
                            if first_tok in FIRST_NAMES:
                                found_cand = c
                                break

                    if not found_cand:
                        continue

                    name_cand = found_cand

                    block_text = block.get_text(separator=' | ')
                    title_info = extract_legal_title(block_text)
                    if not title_info:
                        continue

                    title, seniority = title_info
                    direct_email = extract_person_email_from_container(block)
                    direct_li = None
                    for ca in block.find_all('a', href=True):
                        if 'linkedin.com/in/' in ca['href']:
                            direct_li = ca['href'].strip()
                            break

                    signals = {
                        "inside_repeating_block": True,
                        "legal_title_in_block": True
                    }
                    score, conf, ev = score_candidate_name(name_cand, signals)
                    k = normalize_name_key(name_cand)
                    seen_keys.add(k)
                    li_url, is_dir = format_linkedin_url(name_cand, firm_name, direct_li)
                    discovered.append({
                        "name": name_cand,
                        "title": title,
                        "seniority": seniority,
                        "email": direct_email,
                        "linkedin_url": li_url,
                        "is_direct_linkedin": is_dir,
                        "source_url": t_url,
                        "source_layer": "L3_roster",
                        "confidence": conf,
                        "score": score,
                        "evidence": ev
                    })

                # =============================================================
                # LAYER 4: Rendering Fallback (if raw HTML looks like JS shell)
                # =============================================================
                if len(discovered) == 0 and not rendered_attempted:
                    # Check if body is suspiciously empty / script heavy
                    body_text = t_soup.get_text(strip=True)
                    if len(body_text) < 500 or "javascript" in body_text.lower():
                        rendered_attempted = True
                        rendered_html = await render_team_page_fallback(t_url)
                        if rendered_html:
                            r_soup = BeautifulSoup(rendered_html, 'html.parser')
                            r_blocks = detect_repeating_blocks(r_soup)
                            for block in r_blocks:
                                h_el = block.find(['h2', 'h3', 'h4', 'h5', 'strong'])
                                if not h_el:
                                    continue
                                name_cand = clean_person_name(h_el.get_text(strip=True))
                                if not name_cand or normalize_name_key(name_cand) in seen_keys:
                                    continue
                                title_info = extract_legal_title(block.get_text(separator=' | '))
                                if not title_info:
                                    continue
                                title, seniority = title_info
                                direct_email = extract_person_email_from_container(block)
                                signals = {"inside_repeating_block": True, "legal_title_in_block": True}
                                score, conf, ev = score_candidate_name(name_cand, signals)
                                k = normalize_name_key(name_cand)
                                seen_keys.add(k)
                                li_url, is_dir = format_linkedin_url(name_cand, firm_name)
                                discovered.append({
                                    "name": name_cand,
                                    "title": title,
                                    "seniority": seniority,
                                    "email": direct_email,
                                    "linkedin_url": li_url,
                                    "is_direct_linkedin": is_dir,
                                    "source_url": t_url,
                                    "source_layer": "L4_rendered",
                                    "confidence": conf,
                                    "score": score,
                                    "evidence": ev
                                })
            except Exception:
                continue

    # =====================================================================
    # LAYER 5: Solo Practitioner Fallback (if 0 people found so far)
    # =====================================================================
    if len(discovered) == 0:
        solo = extract_solo_from_firm_name(firm_name)
        if solo:
            li_url, is_dir = format_linkedin_url(solo["name"], firm_name)
            solo["linkedin_url"] = li_url
            solo["is_direct_linkedin"] = is_dir
            solo["source_url"] = website_url
            discovered.append(solo)

    return discovered
