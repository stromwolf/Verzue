from pycasso.constants import width, mask

class ARC4:
    def __init__(self, key):
        self.key = key
        self.keylen = len(self.key)
        self.i = 0
        self.j = 0
        self.S = []

    def main(self):
        # Local refs for performance
        s = self.S
        mask = 255 # Standard ARC4 mask for 256 byte state
        keylen = self.keylen
        key = self.key

        for i in range(256):
            s.append(i)

        j = 0
        for i in range(256):
            t = s[i]
            j = (j + key[i % keylen] + t) & mask
            s[i] = s[j]
            s[j] = t

        self.j = j
        self.i = 0 # Reset i for generation phase

    def g(self, count):
        mask = 255
        i = self.i
        j = self.j
        s = self.S
        r = 0

        while count:
            i = (i + 1) & mask
            t = s[i]
            j = (j + t) & mask
            s[i] = s[j]
            s[j] = t
            r = r * 256 + s[(s[i] + s[j]) & mask]
            count -= 1

        self.i = i
        self.j = j

        return r
