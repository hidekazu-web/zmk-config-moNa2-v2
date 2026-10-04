import re, json, sys
s = open("config/mona2.keymap").read()
km = s[s.index("    keymap {"):]
U = {"ENTER":0x28,"ESC":0x29,"BACKSPACE":0x2A,"TAB":0x2B,"SPACE":0x2C,"MINUS":0x2D,"EQUAL":0x2E,"LBKT":0x2F,"RBKT":0x30,
     "BSLH":0x31,"SEMI":0x33,"SEMICOLON":0x33,"SQT":0x34,"GRAVE":0x35,"COMMA":0x36,"DOT":0x37,"SLASH":0x38,
     "PG_UP":0x4B,"DELETE":0x4C,"PG_DN":0x4E,"RIGHT":0x4F,"LEFT":0x50,"DOWN":0x51,"UP":0x52,
     "LCTRL":0xE0,"LSHFT":0xE1,"LALT":0xE2,"LGUI":0xE3}
for i,c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"): U[c]=0x04+i
for i in range(1,10): U[f"N{i}"]=0x1D+i
U["N0"]=0x27
for i in range(1,13): U[f"F{i}"]=0x39+i
for i in range(13,16): U[f"F{i}"]=0x68+(i-13)
SH={"EXCL":"N1","AT":"N2","HASH":"N3","DLLR":"N4","PRCNT":"N5","CARET":"N6","AMPS":"N7","STAR":"N8","LPAR":"N9","RPAR":"N0",
    "TILDE":"GRAVE","LBRC":"LBKT","RBRC":"RBKT","PIPE":"BSLH","DQT":"SQT","LT":"COMMA","GT":"DOT","PLUS":"EQUAL","UNDER":"MINUS","COLON":"SEMI"}
MOD={"LC":0x01,"LS":0x02,"LA":0x04,"LG":0x08,"RC":0x10,"RS":0x20,"RA":0x40,"RG":0x80}
def code(k):
    m=re.fullmatch(r"([A-Z]{2})\((.*)\)",k)
    if m: return (MOD[m.group(1)]<<24)|code(m.group(2))
    if k in SH: return (0x02<<24)|code(SH[k])
    return (0x07<<16)|U[k]
def binding(tok):
    parts=tok.split()
    b=parts[0]
    if b=="&trans": return ("Transparent",0,0)
    if b=="&kp": return ("Key Press",code(parts[1]),0)
    if b in("&lt","&lt_sp"): return ("Layer-Tap",int(parts[1]),code(parts[2]))
    raise ValueError(tok)
def layer(name):
    m=re.search(r'display-name = "%s";\s*bindings = <(.*?)>;'%name, km, re.S)
    toks=re.findall(r'&[a-z_0-9]+(?:\s+(?![&])[A-Z_0-9()]+)*', m.group(1))
    assert len(toks)==42, (name,len(toks))
    return toks
plan=[]
base=layer("BASE")
for pos in (36,38,39):
    b,p1,p2=binding(base[pos]); plan.append({"layer":0,"pos":pos,"behavior":b,"p1":p1,"p2":p2,"src":base[pos]})
for lid,name in ((1,"SYM"),(2,"NUM"),(3,"NAV")):
    for pos,t in enumerate(layer(name)):
        if lid==2 and pos==39: t="&kp N0"   # DYA 版: 右親指は 0 のみ (BLE はコンボ)
        b,p1,p2=binding(t); plan.append({"layer":lid,"pos":pos,"behavior":b,"p1":p1,"p2":p2,"src":t})
json.dump(plan,open(".local-tools/plan_layers.json","w"),ensure_ascii=False,indent=0)
for e in plan:
    if e["behavior"]!="Transparent": print(e["layer"],e["pos"],e["src"],e["behavior"],hex(e["p1"]),hex(e["p2"]))
print("total",len(plan))
