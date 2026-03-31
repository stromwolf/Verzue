"""
Deobfuscated Piccoma _s.min.js analysis.

After decoding all base64 strings and resolving the array rotation,
here are the key functions:

get_checksum(url):
    return url.split('/').slice(-1)[0]
    # Gets the LAST path segment of the URL (before query string)

get_seed(checksum, expires):
    sum = expires.split('').reduce((acc, d) => acc + parseInt(d), 0)
    shift = sum % checksum.length
    return checksum.slice(-shift) + checksum.slice(0, -shift)
    # Sum digits of expires, rotate checksum by that amount

window.loadPageImage(element):
    id = element.attr('id')
    data_src = element.attr('data-src')
    checksum = get_checksum(data_src.split('?')[0])   # path part
    expires = parse_qs(data_src.split('?')[1])['expires']
    seed = get_seed(checksum, expires)
    drawImg(id, data_src, seed)
    # NOTE: seed goes DIRECTLY to unscrambleImg(img, 50, seed)
    # There is NO dd() call in this flow!

KEY FINDING: The _s.min.js does NOT use dd() at all.
The seed is passed directly to unscrambleImg / shuffleSeed.
dd() is a separate WASM module (window.dd) that may be used in 
a DIFFERENT code path (e.g., the React viewer pcm_web_viewer_react).
"""

# Now let's test what happens when we DON'T run dd() on the seed
def get_checksum_jp(url_path):
    """For JP Piccoma: last path segment before query."""
    return url_path.rstrip('/').split('/')[-1]

def get_checksum_fr(query_params):
    """For FR Piccoma: 'q' query parameter."""
    return query_params.get('q', '')

def get_seed(checksum, expires):
    """Piccoma V30: sum digits of expires, then rotate checksum."""
    if not expires or not checksum:
        return checksum
    digit_sum = sum(int(d) for d in str(expires) if d.isdigit())
    shift = digit_sum % len(checksum)
    if shift == 0:
        return checksum
    return checksum[-shift:] + checksum[:-shift]

def dd_transform(input_string):
    """The dd() function from piccoma.py - byte-level transformation."""
    result_bytearray = bytearray()
    for index, byte in enumerate(bytes(input_string, 'utf-8')):
        if index < 3: byte = byte + (1 - 2 * (byte % 2))
        elif 2 < index < 6 or index == 8: pass
        elif index < 10: byte = byte + (1 - 2 * (byte % 2))
        elif 12 < index < 15 or index == 16: byte = byte + (1 - 2 * (byte % 2))
        elif index == len(input_string[:-1]) or index == len(input_string[:-2]): byte = byte + (1 - 2 * (byte % 2))
        else: pass
        result_bytearray.append(byte)
    return str(result_bytearray, 'utf-8')

# Test with a realistic example
# Example image URL: https://piccoma.com/st/images/uViQR1xYabc.../10/abcdef1234567890abcdef1234567890?expires=1711910400&...
checksum = "abcdef1234567890abcdef1234567890"  # 32-char hash
expires = "1711910400"

seed_raw = get_seed(checksum, expires)
seed_with_dd = dd_transform(seed_raw)

print(f"Checksum:        {checksum}")
print(f"Expires:         {expires}")
print(f"Digit sum:       {sum(int(d) for d in expires)}")
print(f"Shift:           {sum(int(d) for d in expires) % len(checksum)}")
print()
print(f"Seed (no dd):    {seed_raw}")
print(f"Seed (with dd):  {seed_with_dd}")
print()
print(f"Are they same?   {seed_raw == seed_with_dd}")
print()

# Now let's check what happens with isupper()
print(f"seed_raw.isupper():    {seed_raw.isupper()}")
print(f"seed_with_dd.isupper(): {seed_with_dd.isupper()}")
print()

# Critical: check what the code currently does at line 368
# if seed and seed.isupper() and Canvas:
# The seed is a hex string like 'abcdef1234...' - this is NOT uppercase!
# So seed.isupper() returns False, and the unscrambling is SKIPPED entirely!
print("=" * 60)
print("CRITICAL BUG FOUND:")
print("The condition 'seed.isupper()' is False for hex checksums!")
print("This means the unscrambling code is NEVER executed.")
print("The images are saved as-is (still scrambled).")
print("=" * 60)
