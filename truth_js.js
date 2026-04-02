// Standalone seedrandom.js implementation (Arc4)
function seedrandom(seed) {
  var width = 256,
      chunks = 6,
      digits = 52,
      startdenom = Math.pow(width, chunks),
      significance = Math.pow(2, digits),
      overflow = significance * 2,
      mask = width - 1;

  var key = [];
  mixkey(seed, key);
  var arc4 = new Arc4(key);

  return function() {
    var n = arc4.g(chunks),
        d = startdenom,
        x = 0;
    while (n < significance) {
      n = (n + x) * width;
      d *= width;
      x = arc4.g(1);
    }
    while (n >= overflow) {
      n /= 2;
      d /= 2;
      x >>>= 1;
    }
    return (n + x) / d;
  };

  function Arc4(key) {
    var t, i, j = 0, s = [];
    for (i = 0; i < width; i++) s[i] = i;
    for (i = 0; i < width; i++) {
      j = (j + s[i] + key[i % key.length]) & mask;
      t = s[i]; s[i] = s[j]; s[j] = t;
    }
    this.g = function(count) {
      var t, r = 0, i = 0, j = 0, s = this.s;
      i = (this.i + 1) & mask;
      j = (this.j + s[i]) & mask;
      t = s[i]; s[i] = s[j]; s[j] = t;
      this.i = i; this.j = j;
      r = s[(s[i] + s[j]) & mask];
      while (--count) {
        i = (i + 1) & mask;
        j = (j + s[i]) & mask;
        t = s[i]; s[i] = s[j]; s[j] = t;
        r = r * width + s[(s[i] + s[j]) & mask];
      }
      this.i = i; this.j = j;
      return r;
    };
    this.i = 0; this.j = 0; this.s = s;
  }

  function mixkey(seed, key) {
    seed = seed + '';
    var j = 0;
    while (j < seed.length) {
      key[mask & j] = mask & (j + seed.charCodeAt(j));
      j++;
    }
  }
}

// Run it!
const rng = seedrandom('abc');
const results = [];
for (let i = 0; i < 10; i++) results.push(rng());
console.log(JSON.stringify(results));
