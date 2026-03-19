
import re

def clean_html_func(h):
    return re.sub(r'\\+"', '"', h).replace('\\/', '/')

def analyze_id_shadowing():
    with open(r'e:\Code Files\Verzue Bot\page 1', 'r', encoding='utf-8') as f:
        h = f.read()
    
    clean_html = clean_html_func(h)
    
    # Scoping
    em = re.search(r'"edges"\s*:\s*\[', clean_html)
    ss = clean_html
    if em:
        s=em.end()-1; d=0;
        for i, c in enumerate(clean_html[s:], s):
            if c=='[': d+=1
            elif c==']':
                d-=1
                if d==0: ss=clean_html[s:i+1]; break;
    
    notat_pattern = r'"notation"\s*:\s*"([^"]+)"'
    matches = list(re.finditer(notat_pattern, ss))
    
    for i, m in enumerate(matches):
        notation = m.group(1).strip()
        start_pos = m.start()
        window = ss[max(0, start_pos - 600) : start_pos + 600]
        
        # Find all "id": matches in this window
        all_ids = []
        for idm in re.finditer(r'"id"\s*:\s*"([^"]+)"', window):
            all_ids.append((idm.start(), idm.group(1)))
        
        # Current bot logic (first match)
        first_id_match = re.search(r'(?<!series)(?<!episode)"id"\s*:\s*"([^"]+)"', window)
        first_id = first_id_match.group(1) if first_id_match else "NONE"
        
        print(f"[{i+1}] {notation}")
        print(f"  First ID found by BOT: {first_id}")
        print(f"  All IDs in window: {all_ids}")
        print("-" * 30)

if __name__ == "__main__":
    analyze_id_shadowing()
