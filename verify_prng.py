import os
import sys

# Add root for imports
sys.path.append(os.getcwd())
try:
    from app.lib.pycasso.prng import seedrandom
except ImportError:
    # Handle direct run if needed
    from app.lib.pycasso.prng import seedrandom

def verify(seed_str):
    print(f"🔬 PRNG Sequence for seed '{seed_str}':")
    rng = seedrandom(seed_str)
    results = []
    for _ in range(20):
        results.append(rng())
    
    print(results)

if __name__ == "__main__":
    verify("abc")
