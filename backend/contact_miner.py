import re
import urllib.parse
import html
import json
from typing import Tuple, List, Optional, Set, Dict, Any
import httpx
from bs4 import BeautifulSoup
import asyncio
import dns.resolver

# Regex for detecting email addresses
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')

# Common file extensions that might get caught in regex
INVALID_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.css', '.js', '.woff', '.woff2', '.ttf'}

# Common freemail providers
FREEMAIL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'aol.com',
    'icloud.com', 'mail.com', 'zoho.com', 'protonmail.com', 'proton.me', 'live.com', 'msn.com'
}

# Common generic prefixes for business mailboxes (English, French, etc.)
GENERIC_PREFIXES = {
    'info', 'contact', 'intake', 'office', 'admin', 'help', 'support',
    'inquiries', 'inquiry', 'frontdesk', 'hello', 'team', 'service',
    'billing', 'general', 'reception', 'law', 'mail', 'attorneys', 'firm',
    'questions', 'connect', 'legal', 'bureau', 'secretariat', 'courriel',
    'direction', 'administration', 'communication', 'greffe'
}

# Domains to filter out (CDNs, analytics, CMS vendors, placeholders)
JUNK_DOMAINS = {
    'sentry.io', 'wixpress.com', 'wix.com', 'cloudflare.com', 'googleapis.com',
    'example.com', 'domain.com', 'schema.org', 'wordpress.org', 'gravatar.com',
    'email.com', 'yoursite.com', 'bootstrapcdn.com', 'fontawesome.com', 'jquery.com',
    'twimg.com', 'facebook.com', 'twitter.com', 'instagram.com', 'linkedin.com',
    'apple.com', 'google.com', 'gstatic.com', 'recaptcha.net', 'trustpilot.com'
}

# Subpage discovery keywords (English, French/Quebec, Spanish)
CONTACT_KEYWORDS = [
    # English
    'contact', 'about', 'team', 'attorney', 'lawyer', 'our-firm', 'people',
    'staff', 'reach-us', 'consultation', 'locations', 'connect', 'partners',
    'professionals', 'leadership',
    # French (Quebec / Canada / Europe)
    'nous-joindre', 'contactez-nous', 'coordonnees', 'a-propos', 'equipe',
    'notre-equipe', 'avocats', 'avocat', 'notaires', 'notaire', 'professionnels',
    'cabinet', 'membres', 'juristes',
    # Spanish
    'contacto', 'equipo', 'abogados', 'nosotros'
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

_mx_cache: Dict[str, bool] = {}

def check_domain_mx(domain: str) -> bool:
    """Synchronous DNS MX record check with local in-memory cache."""
    clean_domain = domain.lower().strip()
    if clean_domain in _mx_cache:
        return _mx_cache[clean_domain]
    try:
        answers = dns.resolver.resolve(clean_domain, 'MX', lifetime=2.5)
        has_mx = len(answers) > 0
    except Exception:
        has_mx = False
    _mx_cache[clean_domain] = has_mx
    return has_mx

def classify_email(email_str: str) -> str:
    """Classifies email into 'freemail', 'generic', or 'personal'."""
    if '@' not in email_str:
        return 'generic'
    user, domain = email_str.split('@', 1)
    user = user.lower()
    domain = domain.lower()
    if domain in FREEMAIL_DOMAINS:
        return 'freemail'
    if user in GENERIC_PREFIXES:
        return 'generic'
    return 'personal'

def decode_cloudflare_email(cf_hex: str) -> Optional[str]:
    """Decodes Cloudflare XOR hex-encoded email protection strings."""
    if not cf_hex or len(cf_hex) < 4:
        return None
    try:
        r = int(cf_hex[:2], 16)
        email = ''.join([chr(int(cf_hex[i:i+2], 16) ^ r) for i in range(2, len(cf_hex), 2)])
        return email.strip()
    except Exception:
        return None

def clean_email(email_str: str) -> Optional[str]:
    """Clean and validate an email candidate."""
    if not email_str:
        return None
    # Unescape HTML entities (e.g. &#109;&#97;...)
    decoded = html.unescape(email_str).strip().lower()
    email = re.sub(r'[.,;:\'\"\)\]\>]+$', '', decoded)
    email = re.sub(r'^[<\(\[\'\"]+', '', email)

    if '@' not in email:
        return None

    parts = email.split('@')
    if len(parts) != 2:
        return None

    user, domain = parts
    if not user or not domain or '.' not in domain:
        return None

    for ext in INVALID_EXTENSIONS:
        if domain.endswith(ext) or user.endswith(ext):
            return None

    domain_base = domain.lower()
    for junk in JUNK_DOMAINS:
        if junk in domain_base:
            return None

    # RFC 5321 length limits: min 4 ('a@b.c'), max 254 chars
    if len(email) < 4 or len(email) > 254:
        return None

    return email

def extract_emails_from_json_ld(soup: BeautifulSoup) -> List[str]:
    """Extracts published emails from Schema.org / JSON-LD scripts."""
    found: List[str] = []
    for script in soup.find_all('script', type='application/ld+json'):
        if not script.string:
            continue
        try:
            data = json.loads(script.string.strip())
            items = []
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):
                if '@graph' in data and isinstance(data['@graph'], list):
                    items.extend(data['@graph'])
                else:
                    items.append(data)

            for item in items:
                if not isinstance(item, dict):
                    continue
                # Direct email
                em = item.get('email')
                if em:
                    if isinstance(em, list):
                        found.extend([str(e) for e in em])
                    elif isinstance(em, str):
                        found.append(em)
                # ContactPoint
                cp = item.get('contactPoint')
                if isinstance(cp, dict) and cp.get('email'):
                    found.append(str(cp.get('email')))
                elif isinstance(cp, list):
                    for c in cp:
                        if isinstance(c, dict) and c.get('email'):
                            found.append(str(c.get('email')))
        except Exception:
            continue
    return found

def detect_contact_form(html_str: str, page_url: str) -> Optional[str]:
    """Detects whether a page has an active contact/intake lead form."""
    if not html_str:
        return None
    soup = BeautifulSoup(html_str, 'html.parser')
    for form in soup.find_all('form'):
        form_text = str(form).lower()
        action = (form.get('action') or "").strip()
        form_id = (form.get('id') or "").lower()
        form_class = ' '.join(form.get('class') or []).lower()

        # Check for contact/intake indicators
        indicators = ['contact', 'consultation', 'inquiry', 'intake', 'message', 'lead', 'wpforms', 'gform', 'ninja-forms', 'formidable', 'wpcf7', 'fluentform']
        if any(k in form_text or k in action.lower() or k in form_id or k in form_class for k in indicators):
            if action and not action.startswith('#') and not action.startswith('javascript:'):
                return urllib.parse.urljoin(page_url, action)
            return page_url
    return None

def extract_emails_from_html(html_str: str, target_domain: str = "") -> List[str]:
    """Extract emails with Cloudflare de-obfuscation, entity unescaping, JSON-LD, and regex."""
    found: Set[str] = set()
    soup = BeautifulSoup(html_str, 'html.parser')

    # Priority 1: Cloudflare Email Protection
    for tag in soup.find_all(attrs={'data-cfemail': True}):
        decoded = decode_cloudflare_email(tag['data-cfemail'])
        clean = clean_email(decoded) if decoded else None
        if clean:
            found.add(clean)

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if '/cdn-cgi/l/email-protection#' in href:
            cf_hex = href.split('#')[-1]
            decoded = decode_cloudflare_email(cf_hex)
            clean = clean_email(decoded) if decoded else None
            if clean:
                found.add(clean)
        elif href.lower().startswith('mailto:'):
            # Priority 2: mailto links (unescape HTML entities)
            unescaped = html.unescape(href)
            raw = unescaped[7:].split('?')[0].strip()
            for single in re.split(r'[,;]', raw):
                clean = clean_email(single)
                if clean:
                    found.add(clean)

    # Priority 3: JSON-LD Schema.org metadata
    json_ld_emails = extract_emails_from_json_ld(soup)
    for em in json_ld_emails:
        clean = clean_email(em)
        if clean:
            found.add(clean)

    # Priority 4: Explicit footer / header container inspection
    for container in soup.find_all(['footer', 'header', 'address']):
        c_text = container.get_text(separator=' ')
        for match in EMAIL_REGEX.finditer(c_text):
            clean = clean_email(match.group(0))
            if clean:
                found.add(clean)

    # Priority 5: Full page text regex matches
    text = soup.get_text(separator=' ')
    for match in EMAIL_REGEX.finditer(text):
        clean = clean_email(match.group(0))
        if clean:
            found.add(clean)

    # Score emails: prioritize matching target domain and reputable prefixes
    def score_email(e: str) -> int:
        score = 0
        if target_domain and target_domain in e:
            score += 10
        if any(e.startswith(prefix + '@') for prefix in ['info', 'contact', 'intake', 'office', 'help', 'admin', 'law', 'inquiry']):
            score += 5
        return score

    sorted_emails = sorted(list(found), key=score_email, reverse=True)
    return sorted_emails

async def discover_contact_links(base_url: str, html_str: str) -> List[str]:
    """Find internal links likely pointing to contact or attorney directory pages."""
    links: Set[str] = set()
    soup = BeautifulSoup(html_str, 'html.parser')
    parsed_base = urllib.parse.urlparse(base_url)
    base_netloc = parsed_base.netloc.lower().replace('www.', '')

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript:') or href.startswith('tel:'):
            continue

        resolved = urllib.parse.urljoin(base_url, href)
        parsed_res = urllib.parse.urlparse(resolved)
        res_netloc = parsed_res.netloc.lower().replace('www.', '')

        if res_netloc == base_netloc:
            path_lower = parsed_res.path.lower()
            text_lower = a.get_text(strip=True).lower()

            if any(k in path_lower or k in text_lower for k in CONTACT_KEYWORDS):
                links.add(resolved)

        if len(links) >= 3:
            break

    return list(links)

async def enrich_firm_contact(website_url: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[bool], List[Dict[str, Any]], str, Optional[str], Optional[str]]:
    """
    Crawls the firm website to extract contact emails and detect contact forms.
    Returns:
    (primary_email, email_type, mx_valid, secondary_emails_list, status, final_resolved_website_url, contact_form_url)
    """
    if not website_url:
        return None, None, None, [], "no_website", None, None

    url = website_url.strip()
    if url.startswith("/"):
        url = "https://www.google.com" + url
    elif not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    # Unwrap Google redirect or ad tracking URLs
    if "google.com" in url or "google." in url:
        parsed_g = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed_g.query)
        target_q = qs.get("q", [None])[0] or qs.get("url", [None])[0] or qs.get("adurl", [None])[0]
        if target_q and target_q.startswith("http"):
            url = target_q

    final_url = url
    all_emails: List[str] = []
    detected_contact_form: Optional[str] = None

    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, verify=False, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code < 400:
                final_url = str(resp.url)
                parsed = urllib.parse.urlparse(final_url)
                target_domain = parsed.netloc.lower().replace('www.', '')

                homepage_html = resp.text
                all_emails.extend(extract_emails_from_html(homepage_html, target_domain))
                detected_contact_form = detect_contact_form(homepage_html, final_url)

                contact_links = await discover_contact_links(final_url, homepage_html)
                for subpage in contact_links:
                    try:
                        sub_resp = await client.get(subpage)
                        if sub_resp.status_code < 400:
                            sub_emails = extract_emails_from_html(sub_resp.text, target_domain)
                            all_emails.extend(sub_emails)
                            if not detected_contact_form:
                                detected_contact_form = detect_contact_form(sub_resp.text, str(sub_resp.url))
                    except Exception:
                        continue
        except Exception:
            return None, None, None, [], "failed", None, None

    # Deduplicate while preserving order
    deduped: List[str] = []
    for em in all_emails:
        if em not in deduped:
            deduped.append(em)

    if deduped:
        primary_email = deduped[0]
        domain = primary_email.split('@')[-1]
        loop = asyncio.get_event_loop()
        mx_valid = await loop.run_in_executor(None, check_domain_mx, domain)
        email_type = classify_email(primary_email)

        # Structure secondary emails with classification & mx check
        secondary_list: List[Dict[str, Any]] = []
        for sec in deduped[1:6]:
            sec_domain = sec.split('@')[-1]
            sec_mx = await loop.run_in_executor(None, check_domain_mx, sec_domain)
            secondary_list.append({
                "email": sec,
                "email_type": classify_email(sec),
                "mx_valid": sec_mx
            })

        return primary_email, email_type, mx_valid, secondary_list, "email_found", final_url, detected_contact_form
    elif detected_contact_form:
        return None, None, None, [], "contact_form_only", final_url, detected_contact_form
    else:
        return None, None, None, [], "email_not_found", final_url, None
