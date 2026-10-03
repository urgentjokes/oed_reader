#!/usr/bin/env python3
"""
Example usage of the OED library in Python.
"""

from oed import OEDReader

def main():
    # Initialize reader (auto-detects /Volumes/OED2/OED2.DAT or /Volumes/OED/OED2.DAT)
    with OEDReader() as reader:
        print(f"Loaded OED data file: {reader.dat_path}")
        print(f"Total 32KB blocks:    {reader.total_blocks:,}")

        # 1. Look up words
        words = ["serendipity", "computer", "oxford"]
        for word in words:
            print(f"\n{'='*50}\nLookup: {word}\n{'='*50}")
            entries = reader.lookup(word)
            for entry in entries:
                print(entry.format_terminal())

        # 2. Prefix search
        print(f"\n{'='*50}\nPrefix search for 'dictio'\n{'='*50}")
        matches = reader.search_prefix("dictio", max_results=10)
        for m in matches:
            print(f"  • {m}")

        # 3. Random word
        print(f"\n{'='*50}\nRandom word\n{'='*50}")
        rand_entry = reader.random_entry()
        print(rand_entry.format_terminal())

if __name__ == "__main__":
    main()
