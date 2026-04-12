import urllib.parse

def old_calculate_seed(chk, expires):
    if expires.isdigit():
        n = int(expires)
        if n > 0 and len(chk) > 0:
            shift = n % len(chk)
            if shift != 0:
                c_base = str(chk)
                return c_base[-shift:] + c_base[:len(c_base)-shift]
    return chk

def new_calculate_seed(chk, expires):
    if expires:
        # Sum of digits logic
        sum_digits = sum(int(digit) for digit in str(expires) if digit.isdigit())
        if len(chk) > 0:
            shift = sum_digits % len(chk)
            if shift != 0:
                c_base = str(chk)
                return c_base[-shift:] + c_base[:len(c_base)-shift]
    return chk

# Example values
chk = "abcdefghijklmnopqrstuvwxyz123456" # 32 chars
expires = "1711910400" # Sum = 1+7+1+1+9+1+0+4+0+0 = 24

print(f"Original Seed: {chk}")
print(f"Expires: {expires}")

old_seed = old_calculate_seed(chk, expires)
new_seed = new_calculate_seed(chk, expires)

print(f"Old Calculation Seed (Full Int): {old_seed}")
shift_old = int(expires) % len(chk)
print(f"Old Shift: {shift_old}")

print(f"New Calculation Seed (Sum Digits): {new_seed}")
sum_val = sum(int(d) for d in expires)
shift_new = sum_val % len(chk)
print(f"New Shift: {shift_new}")
