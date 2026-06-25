#!/usr/bin/env python3
# Parse a pktmon-exported .pcapng: extract TLS SNI (real HTTPS hostnames) + DNS names,
# flag ad/tracker hosts. Usage: python3 parse_cap.py <file.pcapng>
import sys, struct, collections
if len(sys.argv) < 2:
    print("usage: parse_cap.py <pcapng>"); sys.exit(1)
try:
    data = open(sys.argv[1], 'rb').read()
except Exception as e:
    print("cannot open:", e); sys.exit(0)

def ip6(b): return ':'.join(format(int.from_bytes(b[i:i+2], 'big'), 'x') for i in range(0, 16, 2))

def blocks(d):
    off = 0; n = len(d); en = '<'; lt = {}; idx = 0
    while off + 12 <= n:
        bt = struct.unpack_from('<I', d, off)[0]
        if bt == 0x0A0D0D0A:
            bom = struct.unpack_from('<I', d, off + 8)[0]; en = '<' if bom == 0x1A2B3C4D else '>'
        try: bl = struct.unpack_from(en + 'I', d, off + 4)[0]
        except Exception: break
        if bl < 12 or off + bl > n: break
        if bt == 1:
            lt[idx] = struct.unpack_from(en + 'H', d, off + 8)[0]; idx += 1
        elif bt == 6:
            cl = struct.unpack_from(en + 'I', d, off + 20)[0]; yield d[off + 28:off + 28 + cl]
        elif bt == 3:
            yield d[off + 12:off + bl - 4]
        off += bl

def diss(p):
    try:
        if len(p) < 14: return None
        et = int.from_bytes(p[12:14], 'big'); o = 14
        while et == 0x8100 and o + 4 <= len(p): et = int.from_bytes(p[o + 2:o + 4], 'big'); o += 4
        if et == 0x0800: pr = p[o + 9]; dst = '.'.join(map(str, p[o + 16:o + 20])); l4 = o + (p[o] & 0xf) * 4
        elif et == 0x86DD: pr = p[o + 6]; dst = ip6(p[o + 24:o + 40]); l4 = o + 40
        else: return None
        if pr == 6:
            if l4 + 20 > len(p): return None
            return (dst, 't', int.from_bytes(p[l4 + 2:l4 + 4], 'big'), p[l4 + (((p[l4 + 12] >> 4) & 0xf) * 4):])
        if pr == 17:
            if l4 + 8 > len(p): return None
            return (dst, 'u', int.from_bytes(p[l4 + 2:l4 + 4], 'big'), p[l4 + 8:])
    except Exception: return None

def sni(p):
    try:
        if len(p) < 6 or p[0] != 0x16 or p[5] != 0x01: return None
        q = 9; q += 2 + 32; q += 1 + p[q]; q += 2 + int.from_bytes(p[q:q + 2], 'big'); q += 1 + p[q]
        tot = int.from_bytes(p[q:q + 2], 'big'); q += 2; end = q + tot
        while q + 4 <= min(end, len(p)):
            et = int.from_bytes(p[q:q + 2], 'big'); el = int.from_bytes(p[q + 2:q + 4], 'big'); ed = p[q + 4:q + 4 + el]
            if et == 0 and len(ed) >= 5:
                nl = int.from_bytes(ed[3:5], 'big'); return ed[5:5 + nl].decode('ascii', 'replace')
            q += 4 + el
    except Exception: return None

def dq(p):
    try:
        i = 12; L = []
        while i < len(p):
            l = p[i]
            if l == 0 or l & 0xC0: break
            L.append(p[i + 1:i + 1 + l].decode('ascii', 'replace')); i += 1 + l
        return '.'.join(L) if L else None
    except Exception: return None

AD = ('amazon-adsystem','vungle','applovin','inmobi','moloco','fivecdm','doubleclick','googlesyndication',
 'googleadservices','admob','mopub','adcolony','unity3d','unityads','ironsrc','ironsource','supersonic',
 'pubmatic','mintegral','rayjump','mtgglobals','chartboost','liftoff','pangle','byteoversea','isnssdk',
 '2mdn','adsafe','moatads','appsflyer','adjust.','smaato','tapjoy','startapp','start.io','bidmachine',
 'adsrvr','thetradedesk','prebid','bigabid','yandexad','digitalturbine','kochava','singular','branch.io',
 'tenjin','adtech','adserver','.ads.','ads.','adx.','rtb','dsp','criteo','taboola','outbrain','openx',
 'rubicon','magnite','casalemedia','indexexchange','pubnative','vrvm','aarki','smadex','remerge','jampp',
 'youappi','adikteev','mobfox','adgem','adtiming','kayzen','fyber','inner-active','persona.ly','dataseat')
KNOWN = ('google.com','gstatic.com','googleapis.com','android.com','msedge.net','msn.com','bing.com',
 'microsoft.com','windows.com','steampowered','steamstatic','steamcommunity','steamusercontent','facebook.com',
 'fbcdn','youtube.com','azureedge','footprintdns','datadoghq','akamaized','cloudflare','digicert','gvt2','windowsupdate')
isad = lambda h: any(a in h.lower() for a in AD)
isknown = lambda h: any(k in h.lower() for k in KNOWN)

S = collections.Counter(); SI = {}; D = collections.Counter()
for pk in blocks(data):
    r = diss(pk)
    if not r: continue
    dst, pr, dp, pl = r
    if pr == 't' and dp == 443 and pl:
        h = sni(pl)
        if h: S[h] += 1; SI.setdefault(h, collections.Counter())[dst] += 1
    elif pr == 'u' and dp == 53 and pl:
        nm = dq(pl)
        if nm: D[nm] += 1

print("=== AD/TRACKER hosts (block these) ===")
ad = [h for h in S if isad(h)]
for h in sorted(ad, key=lambda x: -S[x]):
    print(f"  {S[h]:4}  {h}   [{', '.join(i for i,_ in SI.get(h,{}).most_common(3))}]")
if not ad: print("  (none)")
print("\n=== UNKNOWN hosts (not obviously legit — scrutinize) ===")
for h, c in S.most_common():
    if not isad(h) and not isknown(h):
        print(f"  {c:4}  {h}   [{', '.join(i for i,_ in SI.get(h,{}).most_common(2))}]")
print("\n=== all SNI (reference) ===")
for h, c in S.most_common(60):
    print(f"  {c:4}  {h}")
print(f"\nSNI {sum(S.values())} total / {len(S)} distinct | DNS distinct {len(D)}")
