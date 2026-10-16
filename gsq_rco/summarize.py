import re,sys,collections
BPW={"IQ1_S":1.5625,"IQ1_M":1.75,"IQ2_XXS":2.0625,"IQ2_XS":2.3125,"IQ2_S":2.5,"Q2_0":2.25,"Q2_K":2.625,"IQ3_XXS":3.0625,"IQ3_S":3.4375,"Q3_K":3.4375,
     "IQ4_XS":4.25,"IQ4_NL":4.5,"Q4_0":4.5,"Q4_K":4.5,"Q5_K":5.5,"Q5_1":6.0,"Q6_K":6.5625,"Q8_0":8.5,"BF16":16,"F16":16,"F32":32}
def load(v):
    d={}
    for l in open(f"{v}.txt"):
        if l.startswith("#") or ":" not in l: continue
        k,t=l.rsplit(":",1); d[k.strip()]=t.strip()
    return d
vs=sys.argv[1:]
D={v:load(v) for v in vs}
print("layer  "+"  ".join(f"{v:>22}" for v in vs)+"   (gate/up/down exps bpw)")
for L in range(48):
    row=[]
    for v in vs:
        d=D[v]; g,u,dn=(BPW[d[f"blk.{L}.ffn_{p}_exps.weight"]] for p in ("gate","up","down"))
        row.append(f"{g:4.2f}/{u:4.2f}/{dn:4.2f}{' A' if f'blk.{L}.attn_q.weight' in d else '  '}")
    print(f"{L:5}  "+"  ".join(f"{r:>22}" for r in row))
for v in vs:
    d=D[v]
    c=collections.Counter()
    for k,t in d.items():
        if "_exps" in k: cls="exps_"+k.split("ffn_")[1].split("_exps")[0]
        elif "shexp" in k and "inp" not in k: cls="shexp"
        elif re.search(r"attn_(q|k|v|output|qkv|gate)\.weight",k): cls="attn"
        elif "ssm_out" in k: cls="ssm_out"
        elif k in("output.weight","token_embd.weight"): cls=k
        else: continue
        c[(cls,t)]+=1
    by=collections.defaultdict(list)
    for (cls,t),n in sorted(c.items()): by[cls].append(f"{t}x{n}")
    print(f"\n{v}:"); [print(f"   {cls:18} {' '.join(x)}") for cls,x in by.items()]
