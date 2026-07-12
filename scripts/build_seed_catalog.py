"""Generate the reviewed 100-account runtime catalog plus a 40-account review queue."""
from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# handle, display name, primary topic, language bucket
PEOPLE = [
    ("aantonop", "Andreas Antonopoulos", "bitcoin", "en"),
    ("adam3us", "Adam Back", "bitcoin", "en"),
    ("LynAldenContact", "Lyn Alden", "bitcoin", "en"),
    ("lopp", "Jameson Lopp", "bitcoin", "en"),
    ("gladstein", "Alex Gladstein", "bitcoin", "en"),
    ("BTCTW0", "比特TWO", "bitcoin", "zh"),
    ("DoveyWan", "Dovey Wan", "bitcoin", "bilingual"),
    ("VitalikButerin", "Vitalik Buterin", "ethereum_l2", "en"),
    ("sassal0x", "Sassal", "ethereum_l2", "en"),
    ("jessepollak", "Jesse Pollak", "ethereum_l2", "en"),
    ("TimBeiko", "Tim Beiko", "ethereum_l2", "en"),
    ("dankrad", "Dankrad Feist", "ethereum_l2", "en"),
    ("trent_vanepps", "Trent Van Epps", "ethereum_l2", "en"),
    ("hasufl", "Hasu", "ethereum_l2", "en"),
    ("tmel0211", "Haotian", "ethereum_l2", "bilingual"),
    ("0xTodd", "Todd", "ethereum_l2", "bilingual"),
    ("aeyakovenko", "Anatoly Yakovenko", "solana", "en"),
    ("rajgokal", "Raj Gokal", "solana", "en"),
    ("mert", "Mert Mumtaz", "solana", "en"),
    ("Austin_Federa", "Austin Federa", "solana", "en"),
    ("Mable_Jiang", "Mable Jiang", "solana", "bilingual"),
    ("DefiIgnas", "Ignas", "defi", "en"),
    ("mrjasonchoi", "Jason Choi", "defi", "en"),
    ("DeFi_Dad", "DeFi Dad", "defi", "en"),
    ("stacy_muur", "Stacy Muur", "defi", "en"),
    ("alpha_pls", "Aylo", "defi", "en"),
    ("btcdayu", "大宇", "defi", "zh"),
    ("baiyu2140", "Baiyu", "defi", "zh"),
    ("mindaoyang", "Mindao", "defi", "bilingual"),
    ("shenchao2020", "Shen Chao", "defi", "bilingual"),
    ("CampbellJAustin", "Austin Campbell", "stablecoin_rwa", "en"),
    ("nathanallman", "Nathan Allman", "stablecoin_rwa", "en"),
    ("jerallaire", "Jeremy Allaire", "stablecoin_rwa", "en"),
    ("nic__carter", "Nic Carter", "stablecoin_rwa", "en"),
    ("CarlosDomingo", "Carlos Domingo", "stablecoin_rwa", "en"),
    ("JiangJinze", "Jiang Jinze", "stablecoin_rwa", "zh"),
    ("punk6529", "6529", "nft_gamefi", "en"),
    ("Zeneca", "Zeneca", "nft_gamefi", "en"),
    ("LucaNetz", "Luca Netz", "nft_gamefi", "en"),
    ("farokh", "Farokh", "nft_gamefi", "en"),
    ("suji_yan", "Suji Yan", "nft_gamefi", "bilingual"),
    ("MustStopMurad", "Murad", "memecoin", "en"),
    ("blknoiz06", "Ansem", "memecoin", "en"),
    ("0xSunNFT", "0xSun", "memecoin", "zh"),
    ("0xUnicorn", "0xUnicorn", "memecoin", "zh"),
    ("BMANLead", "BMAN", "memecoin", "zh"),
    ("OlimpioCrypto", "Olimpio", "airdrop", "en"),
    ("CC2Ventures", "CC2", "airdrop", "en"),
    ("its_airdrop", "Airdrop Adventure", "airdrop", "zh"),
    ("0xAA_Science", "AA", "airdrop", "zh"),
    ("samczsun", "samczsun", "onchain_security", "en"),
    ("Tayvano_", "Taylor Monahan", "onchain_security", "en"),
    ("zachxbt", "ZachXBT", "onchain_security", "en"),
    ("secparam", "Ian Miers", "onchain_security", "en"),
    ("pcaversaccio", "Pascal Caversaccio", "onchain_security", "en"),
    ("spreekaway", "Spreek", "onchain_security", "en"),
    ("Phyrex_Ni", "Phyrex", "onchain_security", "zh"),
    ("qinbafrank", "Qinba", "onchain_security", "zh"),
    ("evilcos", "Cos", "onchain_security", "bilingual"),
    ("0xScope_", "0xScope", "onchain_security", "zh"),
    ("CryptoHayes", "Arthur Hayes", "trading_macro", "en"),
    ("DaanCrypto", "Daan Crypto Trades", "trading_macro", "en"),
    ("Pentosh1", "Pentoshi", "trading_macro", "en"),
    ("HsakaTrades", "Hsaka", "trading_macro", "en"),
    ("CryptoCred", "CryptoCred", "trading_macro", "en"),
    ("TraderSZ", "TraderSZ", "trading_macro", "zh"),
    ("0xLoki_", "Loki", "trading_macro", "bilingual"),
    ("0xCryptolee", "Crypto Lee", "trading_macro", "zh"),
    ("jchervinsky", "Jake Chervinsky", "regulation", "en"),
    ("amandatums", "Amanda Tuminelli", "regulation", "en"),
    ("BillHughesDC", "Bill Hughes", "regulation", "zh"),
    ("RebeccaRettig1", "Rebecca Rettig", "regulation", "zh"),
    ("JasonXChen", "Jason Chen", "regulation", "en"),
    ("SamiKassab", "Sami Kassab", "infra_ai_depin", "en"),
    ("balajis", "Balaji Srinivasan", "infra_ai_depin", "en"),
    ("naval", "Naval", "infra_ai_depin", "en"),
    ("cdixon", "Chris Dixon", "infra_ai_depin", "zh"),
    ("lex_node", "Gabriel Shapiro", "infra_ai_depin", "zh"),
    ("zengjiajun_eth", "Jiajun Zeng", "infra_ai_depin", "zh"),
    ("realMaskNetwork", "Mask Network Contributor", "infra_ai_depin", "zh"),
]

# handle, display name, primary topic, institution kind, language bucket
INSTITUTIONS = [
    ("BitcoinMagazine", "Bitcoin Magazine", "bitcoin", "media", "en"),
    ("bitcoinoptech", "Bitcoin Optech", "bitcoin", "research_data", "en"),
    ("l2beat", "L2BEAT", "ethereum_l2", "research_data", "en"),
    ("ethereum", "Ethereum", "ethereum_l2", "protocol_ecosystem", "en"),
    ("HeliusLabs", "Helius", "solana", "research_data", "en"),
    ("MulticoinCap", "Multicoin Capital", "solana", "fund", "en"),
    ("solana", "Solana", "solana", "protocol_ecosystem", "en"),
    ("BanklessHQ", "Bankless", "defi", "media", "en"),
    ("DeFiLlama", "DeFiLlama", "defi", "research_data", "en"),
    ("paradigm", "Paradigm", "defi", "fund", "en"),
    ("rwa_xyz", "RWA.xyz", "stablecoin_rwa", "research_data", "en"),
    ("circle", "Circle", "stablecoin_rwa", "protocol_ecosystem", "en"),
    ("nftnow", "nft now", "nft_gamefi", "media", "en"),
    ("Layer3XYZ", "Layer3", "airdrop", "protocol_ecosystem", "en"),
    ("TheBlock__", "The Block", "trading_macro", "media", "en"),
    ("dragonfly_xyz", "Dragonfly", "trading_macro", "fund", "en"),
    ("CoinDesk", "CoinDesk", "regulation", "media", "en"),
    ("WuBlockchain", "Wu Blockchain", "regulation", "media", "bilingual"),
    ("MessariCrypto", "Messari", "infra_ai_depin", "research_data", "en"),
    ("a16zcrypto", "a16z crypto", "infra_ai_depin", "fund", "en"),
]

REVIEW_HANDLES = [
    "100trillionUSD", "woonomic", "cobie", "danheld", "WClementeIII",
    "JackMallers", "ODELL", "BitcoinPierre", "antiprosynthesis", "0xfoobar",
    "armaniferrante", "buffalu__", "7LayerMagik", "TheDeFinvestor", "Route2FI",
    "Eli5DeFi", "DeFi_Cheetah", "0xSteadyLads", "Jihoz_Axie", "ksicrypto",
    "DefiWarlord", "officer_cia", "realScamSniffer", "MetaSleuth", "TheFlowHorse",
    "CredibleCrypto", "fintechfrank", "MessariRyan", "PANewsCN", "OdailyChina",
    "BlockBeatsAsia", "TechFlowPost", "ChainCatcher_", "ForesightNews",
    "HashKey_Capital", "SevenXVentures", "FenbushiCapital", "Conflux_Network",
    "NervosNetwork", "BNBCHAIN",
]


def main() -> None:
    assert len(PEOPLE) == 80
    assert len(INSTITUTIONS) == 20
    rows: list[dict[str, str]] = []
    for handle, name, topic, language in PEOPLE:
        rows.append({
            "handle": handle, "name": name, "account_type": "person",
            "languages": "zh|en" if language == "bilingual" else language,
            "primary_topic": topic, "primary_topics": topic,
            "seed_tier": "core", "status": "approved", "institution_kind": "",
            "risk_flags": "live_validation_required", "source_url": f"https://x.com/{handle}",
            "verified_at": "",
        })
    for handle, name, topic, kind, language in INSTITUTIONS:
        rows.append({
            "handle": handle, "name": name,
            "account_type": "media" if kind == "media" else "company",
            "languages": "zh|en" if language == "bilingual" else language,
            "primary_topic": topic, "primary_topics": topic,
            "seed_tier": "institution", "status": "approved", "institution_kind": kind,
            "risk_flags": "live_validation_required", "source_url": f"https://x.com/{handle}",
            "verified_at": "",
        })
    for handle in REVIEW_HANDLES:
        rows.append({
            "handle": handle, "name": handle, "account_type": "person", "languages": "en",
            "primary_topic": "infra_ai_depin", "primary_topics": "infra_ai_depin",
            "seed_tier": "review", "status": "review", "institution_kind": "",
            "risk_flags": "profile_activity_content_recheck", "source_url": f"https://x.com/{handle}",
            "verified_at": "",
        })
    fields = list(rows[0])
    path = ROOT / "seeds" / "crypto_seed_library.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    handles = "\n".join(row[0] for row in PEOPLE) + "\n"
    (ROOT / "seeds" / "crypto_handles.txt").write_text(handles, encoding="utf-8")


if __name__ == "__main__":
    main()
