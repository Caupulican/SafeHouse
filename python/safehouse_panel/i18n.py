"""Every sentence the panel can say, in both languages, in one place.

The parent reads this panel in Brazilian Portuguese; the repo, the logs and the tests
work in English. Rather than the server picking a language (and the log and the page
disagreeing), human-facing text travels as a **message**: ``{"k": key}`` or
``{"k": key, "p": {param: value}}``, built by `msg`. The page renders messages in the
reader's language from the catalog below; `render` turns the same message back into
English for `panel.log`, the console, and the tests.

Rules that keep this honest:

* **The catalog lives here and nowhere else.** `page.py` serialises it into the HTML;
  nothing else carries a translation.
* **Both languages by construction.** `_STRINGS` maps each key to an ``(en, pt)`` pair,
  so a key cannot exist in one language and not the other.
* **Status codes are not translated here.** ACTIVE/OFF/DEGRADED/UNKNOWN and friends stay
  English in the data - CSS classes, tests and the API key off them - and the ``st.*``
  entries below are only their display words.
* **Params may be messages.** ``msg("clamp.high", field=msg("field.unlock"), ...)``
  renders the field name in the reader's language too. Params are escaped by the page;
  templates are trusted and may carry ``<b>`` / ``<code>``.
* **Text quoted from the machine passes through.** A watchdog step's command output, an
  unrecognised HRESULT, a config defect from a guard: shown verbatim, deliberately.
"""

from __future__ import annotations

import re

_PARAM = re.compile(r"\{(\w+)\}")


def msg(key: str, **params) -> dict:
    """A translatable message: a stable key plus the values the sentence needs."""
    return {"k": key, "p": params} if params else {"k": key}


def render(value: object, lang: str = "en") -> str:
    """A message (or string, or list of either) as plain text in `lang`.

    Mirrors the page's own formatter: strings pass through, lists join with spaces,
    unknown keys render as themselves rather than raising - a missing translation must
    never take down a log line or an error response.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(part for part in (render(v, lang) for v in value) if part)
    if not isinstance(value, dict):
        return str(value)
    key = str(value.get("k", ""))
    catalog = CATALOG.get(lang) or CATALOG["en"]
    template = catalog.get(key) or CATALOG["en"].get(key) or key
    params = value.get("p") or {}
    return _PARAM.sub(lambda m: render(params.get(m.group(1), ""), lang), template)


# --- the catalog --------------------------------------------------------------
# key: (English, Brazilian Portuguese). pt-BR is what the parent reads by default, so it
# is written for a parent - "você", plain words, no literal calques.

_STRINGS: dict[str, tuple[str, str]] = {
    # status words. The CODE stays English in the data; this is only what is printed
    # inside the pill.
    "st.ACTIVE": ("ACTIVE", "ATIVO"),
    "st.OFF": ("OFF", "DESLIGADO"),
    "st.DEGRADED": ("DEGRADED", "DEGRADADO"),
    "st.UNKNOWN": ("UNKNOWN", "DESCONHECIDO"),
    "st.BLOCKED": ("BLOCKED", "BLOQUEADO"),
    "st.ALLOWED": ("ALLOWED", "LIBERADO"),
    "st.PARTIAL": ("PARTIAL", "PARCIAL"),
    "st.REWRITTEN": ("REWRITTEN", "REESCRITO"),

    # scheduled-task states ("tarefa" is feminine) and last-result codes
    "ts.Unknown": ("unknown", "desconhecido"),
    "ts.Disabled": ("disabled", "desativada"),
    "ts.Queued": ("queued", "na fila"),
    "ts.Ready": ("ready", "pronta"),
    "ts.Running": ("running", "em execução"),
    "ts.NotRegistered": ("not registered", "não registrada"),
    "task.res.ok": ("OK", "OK"),
    "task.res.fail1": ("the program returned 1 (a failure)",
                       "o programa retornou 1 (uma falha)"),
    "task.res.running": ("currently running", "em execução agora"),
    "task.res.never": ("has never run", "nunca foi executada"),
    "task.res.ended": ("the last run was ended by hand",
                       "a última execução foi encerrada manualmente"),
    "task.res.dup": ("an instance was already running",
                     "já havia uma instância em execução"),
    "task.res.notfound": ("the program could not be found",
                          "o programa não foi encontrado"),
    "task.res.unknown": ("unknown", "desconhecido"),
    "task.res.noquery": ("could not query Task Scheduler",
                         "não foi possível consultar o Agendador de Tarefas"),
    "task.res.notask": ("no such task", "a tarefa não existe"),

    # enforcement lanes
    "lane.roblox": ("Roblox", "Roblox"),
    "lane.browser": ("Web browsing", "Navegador"),
    "lane.youtube": ("YouTube", "YouTube"),
    "lane.how.roblox": ("ends the game process", "encerra o processo do jogo"),
    "lane.how.browser": ("closes any browser process",
                         "fecha qualquer navegador aberto"),
    "lane.reason.unknown": ("could not read Task Scheduler, so it is not known whether "
                            "this lane is running",
                            "não foi possível ler o Agendador de Tarefas, então não dá "
                            "para saber se esta proteção está rodando"),
    "lane.reason.notask": ("no {task} task is registered - nothing is enforcing this "
                           "lane",
                           "a tarefa {task} não está registrada - nada está aplicando "
                           "esta proteção"),
    "lane.reason.disabled": ("the scheduled task is disabled",
                             "a tarefa agendada está desativada"),
    "lane.reason.lastrun": ("the task's last run reported {result}",
                            "a última execução da tarefa reportou {result}"),
    "lane.reason.enforcing": ("enforcing", "em vigor"),

    # facts (label column of the lane cards)
    "fact.budget": ("Daily budget", "Limite diário"),
    "fact.bonus": ("Bonus today", "Bônus de hoje"),
    "fact.used": ("Used today", "Usado hoje"),
    "fact.left": ("Left today", "Restante hoje"),
    "fact.closed": ("Closed today", "Fechamentos hoje"),
    "fact.enforcement": ("Enforcement", "Aplicação"),
    "fact.unlock": ("Unlock", "Liberação"),
    "fact.notify": ("Tells the child", "Avisa a criança"),

    # small value words
    "v.min": ("{n} min", "{n} min"),
    "v.minsec": ("{m} min {s}s", "{m} min {s}s"),
    "v.minsec_left": ("{m} min {s}s left", "faltam {m} min {s}s"),
    "v.none": ("none", "nenhum"),
    "v.yes": ("yes", "sim"),
    "v.no": ("no", "não"),
    "v.off": ("off", "desligado"),
    "v.on": ("on", "ativa"),
    "v.unset": ("unset", "não definido"),
    "v.never": ("never", "nunca"),
    "v.unknown": ("unknown", "desconhecido"),
    "v.notset": ("not set", "não definida"),
    "v.ok": ("ok", "ok"),
    "v.want": ("want {x}", "esperado {x}"),
    "v.allow": ("(allow)", "(permitir)"),
    "v.adguard": ("AdGuard {v}", "AdGuard {v}"),
    "v.rules": ("{n} ({rules} rules)", "{n} ({rules} regras)"),

    # the Roblox lane
    "roblox.note.bonus": ("a bonus of {min} min was granted for today; it expires at "
                          "midnight",
                          "um bônus de {min} min foi concedido para hoje; expira à "
                          "meia-noite"),
    "roblox.note.zero": ("zero minutes: the game is closed within about a minute of "
                         "launching",
                         "zero minutos: o jogo é fechado cerca de um minuto depois de "
                         "abrir"),

    # the browser lane
    "browser.note.limit": ("a daily budget of {min} min is written in the config, but "
                           "the browser lane does not meter yet - it closes every "
                           "browser it finds. Metering is separate work.",
                           "um limite diário de {min} min está gravado na configuração, "
                           "mas esta proteção ainda não mede o tempo - ela fecha todo "
                           "navegador que encontrar. A medição é um trabalho à parte."),
    "browser.note.silent": ("browsers close silently: the child is not told what closed "
                            "them",
                            "os navegadores fecham em silêncio: a criança não fica "
                            "sabendo o que os fechou"),
    "browser.v.closes": ("closes every browser", "fecha todo navegador"),
    "browser.v.limit_inert": ("{n} min (not enforced yet)",
                              "{n} min (ainda não aplicado)"),
    "browser.v.nolimit": ("none written", "nenhum definido"),
    "browser.reason.off": ("the guard is disabled in browser-guard.json",
                           "a proteção está desativada em browser-guard.json"),
    "browser.reason.unlocked": ("a parent unlock is in force for another {m} min {s}s - "
                                "browsers are not being closed",
                                "uma liberação dos pais está em vigor por mais "
                                "{m} min {s}s - os navegadores não estão sendo fechados"),

    # the filtering resolver (WSL)
    "resolver.label": ("Filtering resolver (WSL)", "Resolvedor de filtragem (WSL)"),
    "resolver.reason.ok": ("{domain} comes back sinkholed, so the chain is blocking",
                           "{domain} volta bloqueado, então a cadeia está filtrando"),
    "resolver.reason.ok_nocontrol": ("{domain} comes back sinkholed, so the chain is "
                                     "blocking - but the control name did not resolve, "
                                     "which usually means the resolver is refusing "
                                     "everything rather than filtering well",
                                     "{domain} volta bloqueado, então a cadeia está "
                                     "filtrando - mas o nome de controle não resolveu, "
                                     "o que normalmente significa que o resolvedor está "
                                     "recusando tudo em vez de filtrar direito"),
    "resolver.reason.dead": ("nothing is answering DNS on this machine at all - not "
                             "even the relay. This is the one failure the child would "
                             "actually notice",
                             "nada está respondendo DNS nesta máquina - nem o relay. "
                             "Esta é a única falha que a criança realmente percebe"),
    "resolver.reason.vmdown": ("the WSL virtual machine is not running, so nothing is "
                               "filtered - and the relay falls back to the router, so "
                               "nothing else looks wrong",
                               "a máquina virtual WSL não está rodando, então nada é "
                               "filtrado - e o relay recorre ao roteador, então nada "
                               "mais parece errado"),
    "resolver.reason.failopen": ("DNS is answering but NOTHING IS BEING BLOCKED. This "
                                 "is the failure that looks healthy: the relay has "
                                 "fallen back to the router, so browsing works with no "
                                 "filtering behind it",
                                 "O DNS responde, mas NADA ESTÁ SENDO BLOQUEADO. É a "
                                 "falha que parece saudável: o relay recorreu ao "
                                 "roteador, então a navegação funciona sem nenhum "
                                 "filtro por trás"),
    "resolver.fact.mustblock": ("{d} (must be blocked)", "{d} (deve ser bloqueado)"),
    "resolver.fact.mustresolve": ("{d} (must resolve)", "{d} (deve resolver)"),
    "resolver.fact.adguard": ("AdGuard Home (tcp 3000)", "AdGuard Home (tcp 3000)"),
    "resolver.fact.pihole": ("Pi-hole (tcp 5353)", "Pi-hole (tcp 5353)"),
    "resolver.fact.vm": ("WSL virtual machine", "Máquina virtual WSL"),
    "resolver.fact.lastrun": ("Last watchdog run", "Última ação do vigia"),
    "resolver.v.blocked": ("blocked", "bloqueado"),
    "resolver.v.leak": ("RESOLVED - not filtered", "RESOLVEU - sem filtro"),
    "resolver.v.resolves": ("resolves", "resolve"),
    "resolver.v.noresolve": ("did not resolve", "não resolveu"),
    "resolver.v.listening": ("listening", "respondendo"),
    "resolver.v.noanswer": ("no answer", "sem resposta"),
    "resolver.v.running": ("running", "rodando"),
    "resolver.v.notrunning": ("not running", "parada"),
    "resolver.v.unreadable": ("could not be read", "não foi possível ler"),
    "resolver.v.lastrun": ("{stamp} ({action})", "{stamp} ({action})"),
    "resolver.v.nothing": ("nothing needed doing", "nada precisou ser feito"),
    "resolver.note.half": ("Filtering is working, but one of the two resolvers is not "
                           "listening. Blocking is coming from whichever is left, so "
                           "part of the coverage - the Pi-hole gravity lists, or "
                           "AdGuard's categories - is not in the path.",
                           "A filtragem está funcionando, mas um dos dois resolvedores "
                           "não está respondendo. O bloqueio está vindo do que sobrou, "
                           "então parte da cobertura - as listas do Pi-hole, ou as "
                           "categorias do AdGuard - está fora do caminho."),
    "resolver.note.norecord": ("The watchdog has not written a record yet, so this is "
                               "the panel's own probe rather than the watchdog's "
                               "account of a repair.",
                               "O vigia ainda não gravou nenhum registro, então esta é "
                               "a sonda do próprio painel, não o relato de um reparo "
                               "feito pelo vigia."),
    "resolver.note.notask": ("No {task} task is registered, so nothing restarts the "
                             "chain by itself. Register it with install.py "
                             "--resolver-only.",
                             "A tarefa {task} não está registrada, então nada reinicia "
                             "a cadeia sozinho. Registre com install.py "
                             "--resolver-only."),
    "resolver.note.disabled": ("{task} is disabled - the chain is up now, but nothing "
                               "will bring it back if it stops.",
                               "{task} está desativada - a cadeia está de pé agora, mas "
                               "nada vai levantá-la de novo se ela parar."),
    "resolver.note.session": ("Repairs run in the logged-on user's session: Windows "
                              "refuses to run WSL as LocalSystem, so nothing can start "
                              "the chain while nobody is signed in to the PC. That is "
                              "also the only time it matters.",
                              "Os reparos rodam na sessão do usuário conectado: o "
                              "Windows se recusa a rodar o WSL como LocalSystem, então "
                              "nada consegue iniciar a cadeia enquanto ninguém está com "
                              "sessão aberta no PC. Que é também o único momento em que "
                              "isso importa."),

    # DNS filtering (AdGuard)
    "dns.reason.noconfig": ("no usable adguard.json on this machine, so the resolver "
                            "cannot be read from here",
                            "não há um adguard.json utilizável nesta máquina, então o "
                            "resolvedor não pode ser lido daqui"),
    "dns.reason.noanswer": ("{url} did not answer. If AdGuard is really down, the relay "
                            "fails OPEN to the router and nothing is being filtered at "
                            "all",
                            "{url} não respondeu. Se o AdGuard estiver mesmo fora do "
                            "ar, o relay falha ABERTO para o roteador e nada está sendo "
                            "filtrado"),
    "dns.reason.protoff": ("protection is switched off in AdGuard Home",
                           "a proteção está desligada no AdGuard Home"),
    "dns.reason.nofilterstatus": ("AdGuard answered, but its filtering status did not",
                                  "o AdGuard respondeu, mas o status de filtragem não"),
    "dns.reason.filteroff": ("filtering is switched off in AdGuard Home",
                             "a filtragem está desligada no AdGuard Home"),
    "dns.reason.filtering": ("filtering", "filtrando"),
    "pihole.v.ms": ("answered in {ms}ms", "respondeu em {ms}ms"),
    "pihole.v.none": ("no answer ({err})", "sem resposta ({err})"),

    # browser policy (HKLM)
    "policy.reason.noreg": ("could not read the registry",
                            "não foi possível ler o registro"),
    "policy.reason.all": ("every browser policy is applied",
                          "todas as políticas de navegador estão aplicadas"),
    "policy.reason.none": ("no browser policy is applied",
                           "nenhuma política de navegador está aplicada"),
    "policy.reason.some": ("some browser policies are missing - a browser without them "
                           "can still resolve names itself and bypass the filter",
                           "algumas políticas de navegador estão faltando - um "
                           "navegador sem elas ainda consegue resolver nomes sozinho e "
                           "contornar o filtro"),

    # the known gaps
    "sev.high": ("high", "alto"),
    "sev.accepted": ("accepted", "aceito"),
    "sev.planned": ("planned", "planejado"),
    "gap.plain-dns-bypass.title": ("Any device can use 8.8.8.8 directly and skip all "
                                   "filtering",
                                   "Qualquer aparelho pode usar o 8.8.8.8 direto e "
                                   "pular toda a filtragem"),
    "gap.plain-dns-bypass.detail": ("Nothing forces DNS through the filter: there is no "
                                    "firewall rule blocking outbound port 53 to "
                                    "anything but the resolver. A program - or a child "
                                    "who reads one web page - can point at 8.8.8.8 and "
                                    "every block on this page stops applying to it.",
                                    "Nada força o DNS a passar pelo filtro: não há "
                                    "regra de firewall bloqueando a porta 53 de saída "
                                    "para nada além do resolvedor. Um programa - ou uma "
                                    "criança que leia uma página na internet - pode "
                                    "apontar para o 8.8.8.8, e todo bloqueio deste "
                                    "painel deixa de valer para ele."),
    "gap.relay-fails-open.title": ("When WSL is down, DNS falls back to the router and "
                                   "nothing is filtered",
                                   "Quando o WSL cai, o DNS recorre ao roteador e nada "
                                   "é filtrado"),
    "gap.relay-fails-open.detail": ("The loopback relay fails OPEN by design - "
                                    "resolution keeps working with no filtering behind "
                                    "it, which is why an outage looks exactly like a "
                                    "healthy machine. This is the 2026-08-01 failure: "
                                    "fourteen hours unfiltered with every other signal "
                                    "green. NOW WATCHED, not fixed: the resolver card "
                                    "on the Home tab probes both resolvers directly, "
                                    "and SafeHouse-Resolver restarts them every 15 "
                                    "minutes - but only while somebody is signed in to "
                                    "the PC, because Windows will not run WSL as "
                                    "LocalSystem.",
                                    "O relay local falha ABERTO por design - a "
                                    "resolução continua funcionando sem nenhum filtro "
                                    "por trás, e é por isso que uma pane parece "
                                    "exatamente uma máquina saudável. Foi a falha de "
                                    "2026-08-01: catorze horas sem filtro com todos os "
                                    "outros sinais verdes. HOJE É VIGIADO, não "
                                    "corrigido: o cartão do resolvedor na aba Início "
                                    "sonda os dois resolvedores diretamente, e a "
                                    "SafeHouse-Resolver os reinicia a cada 15 minutos - "
                                    "mas só enquanto alguém está com sessão aberta no "
                                    "PC, porque o Windows não roda o WSL como "
                                    "LocalSystem."),
    "gap.local-admin.title": ("The child has local administrator on this PC",
                              "A criança tem administrador local neste PC"),
    "gap.local-admin.detail": ("By the parent's choice. He can stop either scheduled "
                               "task, edit the config files, or clear the registry "
                               "policy. Nothing here prevents that - the lanes make it "
                               "a visible, deliberate act rather than an impossible "
                               "one, and this panel is one more thing that would have "
                               "to be switched off.",
                               "Por escolha do pai. Ele pode parar as tarefas "
                               "agendadas, editar os arquivos de configuração ou limpar "
                               "a política do registro. Nada aqui impede isso - as "
                               "proteções tornam o ato visível e deliberado em vez de "
                               "impossível, e este painel é mais uma coisa que teria de "
                               "ser desligada."),
    "gap.play-games-lane.title": ("The Google Play Games VM has no process-kill lane",
                                  "A VM do Google Play Games não tem proteção por "
                                  "encerramento de processo"),
    "gap.play-games-lane.detail": ("It runs its own resolver inside crosvm, so it sees "
                                   "neither the hosts file nor a Windows process kill. "
                                   "Only the AdGuard lane reaches it. A third "
                                   "enforcement lane for it is planned and not built.",
                                   "Ela roda o próprio resolvedor dentro do crosvm, "
                                   "então não enxerga nem o arquivo hosts nem um "
                                   "encerramento de processo do Windows. Só a via do "
                                   "AdGuard a alcança. Uma terceira proteção para ela "
                                   "está planejada e ainda não foi construída."),
    "gap.panel-no-tls.title": ("This panel has no TLS - the password crosses the LAN in "
                               "clear",
                               "Este painel não tem TLS - a senha cruza a rede sem "
                               "criptografia"),
    "gap.panel-no-tls.detail": ("Serving HTTPS needs a certificate, and making one "
                                "needs tooling this vendored Python does not have. "
                                "Anyone already able to sniff this network can read "
                                "the login. It is on the house LAN only, never the "
                                "internet, and it is not port-forwarded.",
                                "Servir HTTPS exige um certificado, e criar um exige "
                                "ferramentas que este Python embarcado não tem. Quem já "
                                "consegue espiar esta rede pode ler o login. Ele fica "
                                "só na rede da casa, nunca na internet, e não tem "
                                "redirecionamento de porta."),

    # the on-demand probes
    "probe.ads.label": ("Ad network", "Rede de anúncios"),
    "probe.ads.why": ("the ad and tracker lists are loaded and matching",
                      "as listas de anúncios e rastreadores estão carregadas e "
                      "funcionando"),
    "probe.youtube.label": ("YouTube", "YouTube"),
    "probe.youtube.why": ("YouTube resolves normally - the standing block was lifted "
                          "on 2026-08-26 and nothing is putting it back",
                          "o YouTube resolve normalmente - o bloqueio permanente foi "
                          "retirado em 26/08/2026 e nada o recoloca"),
    "probe.adult.label": ("Adult content", "Conteúdo adulto"),
    "probe.adult.why": ("the NSFW list is loaded and matching",
                        "a lista NSFW está carregada e funcionando"),
    "probe.control.label": ("Control (must resolve)", "Controle (deve resolver)"),
    "probe.control.why": ("the resolver is answering at all - without this, everything "
                          "blocked looks like everything working",
                          "o resolvedor está respondendo - sem isto, tudo bloqueado "
                          "pareceria tudo funcionando"),
    "probe.detail.nodomain": ("no domain configured for this probe - set "
                              "\"probes\": {\"{probe}\": \"<domain>\"} in panel.json",
                              "nenhum domínio configurado para esta sonda - defina "
                              "\"probes\": {\"{probe}\": \"<domínio>\"} em panel.json"),
    "probe.detail.nolog": ("the query did not reach AdGuard (nothing in its query "
                           "log). Either the resolver is not in the path, or the query "
                           "log is off.",
                           "a consulta não chegou ao AdGuard (nada no log de "
                           "consultas). Ou o resolvedor está fora do caminho, ou o log "
                           "de consultas está desligado."),
    "probe.detail.reason": ("AdGuard reason: {reason}", "Motivo do AdGuard: {reason}"),

    # control validation and clamping
    "err.notint": ("{field} must be a whole number, got {got}",
                   "{field} precisa ser um número inteiro; veio {got}"),
    "err.notbool": ("{field} must be true or false, got {got}",
                    "{field} precisa ser sim ou não; veio {got}"),
    "clamp.low": ("{field} {got} is below the minimum, applied {applied}",
                  "{field} {got} está abaixo do mínimo; foi aplicado {applied}"),
    "clamp.high": ("{field} {got} is above the maximum, applied {applied}",
                   "{field} {got} está acima do máximo; foi aplicado {applied}"),
    "err.delayneeds": ("{label} needs a min and a max",
                       "{label} precisa de um mínimo e um máximo"),
    "field.roblox_limit": ("Roblox daily minutes", "Minutos diários de Roblox"),
    "field.roblox_bonus": ("Roblox bonus minutes", "Minutos de bônus de Roblox"),
    "field.browser_limit": ("Browser daily minutes", "Minutos diários de navegador"),
    "field.unlock": ("Unlock minutes", "Minutos de liberação"),
    "field.browser_enabled": ("Browser guard enabled", "Proteção do navegador"),
    "field.notify": ("Notify", "Aviso"),
    "field.youtube": ("YouTube blocked", "YouTube bloqueado"),
    "field.roblox_delay": ("Roblox kill delay", "Pausa antes de fechar o Roblox"),
    "field.browser_delay": ("Browser kill delay", "Pausa antes de fechar o navegador"),
    "field.minof": ("{label} minimum", "mínimo de {label}"),
    "field.maxof": ("{label} maximum", "máximo de {label}"),

    # control results
    "ctl.roblox_limit.done": ("Roblox daily budget is now {n} min (was {was}).",
                              "O limite diário de Roblox agora é {n} min (antes: "
                              "{was})."),
    "ctl.roblox_limit.zero": ("Zero means the game is closed within a minute of "
                              "launching.",
                              "Zero significa que o jogo é fechado até um minuto depois "
                              "de abrir."),
    "ctl.bonus.set": ("Roblox bonus for today is now {n} min (was {was}).",
                      "O bônus de Roblox de hoje agora é {n} min (antes: {was})."),
    "ctl.bonus.back": ("Today's Roblox bonus of {was} min has been taken back.",
                       "O bônus de {was} min de hoje foi retirado."),
    "ctl.bonus.midnight": ("It expires at midnight.", "Expira à meia-noite."),
    "ctl.browser.on": ("Browser guard is ENFORCING - any browser is closed within "
                       "about a minute.",
                       "A proteção do navegador está ATIVA - qualquer navegador é "
                       "fechado em cerca de um minuto."),
    "ctl.browser.off": ("Browser guard is OFF. Nothing closes a browser now, and this "
                        "does not expire by itself - use the unlock for a timed pass.",
                        "A proteção do navegador está DESLIGADA. Nada fecha os "
                        "navegadores agora, e isso não expira sozinho - para um passe "
                        "temporário, use a liberação por tempo."),
    "ctl.blimit.stored": ("Stored a browser budget of {n} min/day (was {was}). NOT "
                          "ENFORCED YET - the browser lane still closes every browser; "
                          "metering is separate work.",
                          "Limite de navegação de {n} min/dia gravado (antes: {was}). "
                          "AINDA NÃO APLICADO - a proteção do navegador continua "
                          "fechando todo navegador; a medição é um trabalho à parte."),
    "ctl.unlock.on": ("Browsing is unlocked for {n} min.",
                      "A navegação está liberada por {n} min."),
    "ctl.unlock.off": ("Unlock cancelled - the browser guard is enforcing again.",
                       "Liberação cancelada - a proteção do navegador está ativa de "
                       "novo."),
    "ctl.notify.on": ("The child will now see a message on screen when a browser is "
                      "closed.",
                      "A criança agora verá uma mensagem na tela quando um navegador "
                      "for fechado."),
    "ctl.notify.off": ("Browsers close silently - the child is not told why.",
                       "Os navegadores fecham em silêncio - a criança não fica sabendo "
                       "o porquê."),
    "ctl.delay.set": ("{label} is now {lo}-{hi}s (was {wlo}-{whi}s).",
                      "{label} agora é {lo}-{hi}s (antes: {wlo}-{whi}s)."),
    "ctl.delay.swap": ("the maximum was below the minimum, so both are now the minimum",
                       "o máximo estava abaixo do mínimo, então os dois agora são o "
                       "mínimo"),
    "ctl.yt.noconfig": ("AdGuard Home is not configured on this machine (no usable "
                        "adguard.json), so YouTube cannot be changed at the resolver.",
                        "O AdGuard Home não está configurado nesta máquina (sem um "
                        "adguard.json utilizável), então o YouTube não pode ser "
                        "alterado no resolvedor."),
    "ctl.yt.noanswer": ("AdGuard Home did not answer, so nothing was changed. The "
                        "resolver may be down - check WSL.",
                        "O AdGuard Home não respondeu, então nada foi alterado. O "
                        "resolvedor pode estar fora do ar - verifique o WSL."),
    "ctl.yt.already": ("YouTube was already {state} at the resolver; nothing to "
                       "change.",
                       "O YouTube já estava {state} no resolvedor; nada a alterar."),
    "ctl.yt.refused": ("AdGuard Home refused the change ({r}). YouTube is still "
                       "{state}.",
                       "O AdGuard Home recusou a alteração ({r}). O YouTube continua "
                       "{state}."),
    "ctl.yt.now": ("YouTube is now {state} at the resolver.",
                   "O YouTube agora está {state} no resolvedor."),
    "ctl.yt.reconcile": ("Note: adguard/apply-filters.py re-adds these rules when it "
                         "next runs, so an unblock that must survive a reconcile needs "
                         "the repo changed too.",
                         "Atenção: o adguard/apply-filters.py recoloca essas regras na "
                         "próxima vez que rodar, então um desbloqueio que precise "
                         "durar exige mudar o repositório também."),

    # the YouTube schedule (short day names follow datetime.weekday: 0 = Monday)
    "day.0": ("Mon", "seg"),
    "day.1": ("Tue", "ter"),
    "day.2": ("Wed", "qua"),
    "day.3": ("Thu", "qui"),
    "day.4": ("Fri", "sex"),
    "day.5": ("Sat", "sáb"),
    "day.6": ("Sun", "dom"),
    "yt.mode.blocked": ("Always blocked", "Sempre bloqueado"),
    "yt.mode.schedule": ("Allowed windows", "Janelas permitidas"),
    "yt.mode.allowed": ("Allowed", "Liberado"),
    "yt.win.add": ("Add a window", "Adicionar janela"),
    "yt.win.remove": ("Remove", "Remover"),
    "field.youtube_mode": ("YouTube at the resolver", "YouTube no resolvedor"),
    "sched.next.allow": ("Opens {day} {t}", "Libera {day} {t}"),
    "sched.next.block": ("Blocks {day} {t}", "Bloqueia {day} {t}"),
    "chip.youtube.sched": ("by schedule", "por janela"),
    "k.yt.sched": ("Schedule", "Janelas"),
    "sched.defect": ("youtube-schedule.json could not be read ({why}) - treated as "
                     "always blocked until it is fixed",
                     "não foi possível ler o youtube-schedule.json ({why}) - tratado "
                     "como sempre bloqueado até ser corrigido"),
    "err.sched.shape": ("The YouTube schedule needs a mode and a list of windows.",
                        "A programação do YouTube precisa de um modo e de uma lista "
                        "de janelas."),
    "err.sched.mode": ("Unknown YouTube mode {got}.",
                       "Modo de YouTube desconhecido: {got}."),
    "err.sched.count": ("At most {max} windows.", "No máximo {max} janelas."),
    "err.sched.win": ("Each window needs its days, a start and an end.",
                      "Cada janela precisa dos dias, de um início e de um fim."),
    "err.sched.days": ("Each window needs at least one valid weekday.",
                       "Cada janela precisa de pelo menos um dia da semana válido."),
    "err.sched.time": ("Times must be HH:MM; got {got}.",
                       "Os horários precisam estar em HH:MM; veio {got}."),
    "err.sched.same": ("A window cannot start and end at the same time.",
                       "Uma janela não pode começar e terminar no mesmo horário."),
    "err.sched.halftime": ("A window needs both times, or neither (empty means the "
                           "whole day).",
                           "Uma janela precisa dos dois horários, ou de nenhum (em "
                           "branco significa o dia todo)."),
    "err.sched.ovr": ("The stored override is malformed.",
                      "A liberação manual gravada está malformada."),
    "err.ovr.state": ("The override must be ALLOWED, BLOCKED, or cleared.",
                      "A ação manual precisa ser liberar, bloquear ou voltar ao "
                      "horário."),
    "ctl.sched.blocked": ("YouTube is set to always blocked.",
                          "O YouTube agora fica sempre bloqueado."),
    "ctl.sched.allowed": ("YouTube is set to allowed at any time.",
                          "O YouTube agora fica liberado a qualquer hora."),
    "ctl.sched.schedule": ("YouTube now follows the allowed windows: blocked except "
                           "inside them.",
                           "O YouTube agora segue as janelas permitidas: bloqueado "
                           "fora delas."),
    "ctl.sched.nowin": ("No windows are defined yet, so this stays always blocked "
                        "until you add one.",
                        "Nenhuma janela foi definida ainda, então fica sempre "
                        "bloqueado até você adicionar uma."),
    "ctl.sched.retry": ("The schedule was saved, but the resolver could not be "
                        "updated right now; the panel retries every minute until it "
                        "answers.",
                        "A programação foi salva, mas o resolvedor não pôde ser "
                        "atualizado agora; o painel tenta de novo a cada minuto até "
                        "ele responder."),
    "ctl.ovr.noschedule": ("There is no active schedule to override - choose "
                           "\"Allowed windows\" first.",
                           "Não há um horário ativo para substituir - escolha "
                           "\"Janelas permitidas\" primeiro."),
    "ctl.ovr.allowed": ("YouTube is allowed manually until {t}; then the schedule "
                        "takes over again.",
                        "O YouTube está liberado manualmente até {t}; depois o "
                        "horário volta a valer."),
    "ctl.ovr.blocked": ("YouTube is blocked manually until {t}; then the schedule "
                        "takes over again.",
                        "O YouTube está bloqueado manualmente até {t}; depois o "
                        "horário volta a valer."),
    "ctl.ovr.cleared": ("Back on the schedule.", "De volta ao horário."),
    "sched.ovr.allowed": ("Allowed manually - back on the schedule at {t}",
                          "Liberado manualmente - volta ao horário às {t}"),
    "sched.ovr.blocked": ("Blocked manually - back on the schedule at {t}",
                          "Bloqueado manualmente - volta ao horário às {t}"),
    "b.yt.allownow": ("Allow now", "Liberar agora"),
    "b.yt.blocknow": ("Block now", "Bloquear agora"),
    "b.yt.backsched": ("Back to the schedule", "Voltar ao horário"),
    "yt.win.allday": ("Leave the times empty to allow the whole day.",
                      "Deixe os horários em branco para liberar o dia todo."),
    "ctl.resolver.unreachable": ("The resolver watchdog cannot be reached from here.",
                                 "O vigia do resolvedor não pode ser acionado daqui."),
    "ctl.resolver.refused": ("the scheduler refused the request",
                             "o agendador recusou o pedido"),
    "ctl.resolver.failed": ("Could not start {task}: {detail} Two things cause this: "
                            "the task is not registered (install.py --resolver-only), "
                            "or nobody is signed in to the PC - WSL cannot be started "
                            "without a user session.",
                            "Não foi possível iniciar {task}: {detail} Duas coisas "
                            "causam isso: a tarefa não está registrada (install.py "
                            "--resolver-only), ou ninguém está com sessão aberta no PC "
                            "- o WSL não inicia sem uma sessão de usuário."),
    "ctl.resolver.asked": ("Asked the resolver watchdog to run. Starting WSL and both "
                           "containers takes up to a minute; this page will show them "
                           "answering when they do. If it still says down after a "
                           "minute, nobody is signed in to the PC or WSL itself is "
                           "refusing to start.",
                           "O vigia do resolvedor foi acionado. Iniciar o WSL e os "
                           "dois contêineres leva até um minuto; esta página vai "
                           "mostrar quando eles responderem. Se depois de um minuto "
                           "ainda constar fora do ar, ninguém está com sessão aberta "
                           "no PC ou o próprio WSL está se recusando a iniciar."),

    # HTTP-level errors and confirmations
    "err.host": ("This is not a name this panel answers to.",
                 "Este não é um nome que este painel atende."),
    "err.cross.login": ("This request did not come from the panel's own page.",
                        "Esta requisição não veio da própria página do painel."),
    "err.cross": ("Cross-site request refused ({why}). Nothing was changed.",
                  "Requisição de outro site recusada ({why}). Nada foi alterado."),
    "err.cross.pw": ("Cross-site request refused ({why}). The password was not "
                     "changed.",
                     "Requisição de outro site recusada ({why}). A senha não foi "
                     "alterada."),
    "err.nopw": ("No password is set on this panel yet. Run `python -m safehouse_panel "
                 "set-password` on the machine.",
                 "Nenhuma senha foi definida neste painel ainda. Rode `python -m "
                 "safehouse_panel set-password` na máquina."),
    "err.lockout": ("Too many failed attempts. Try again in {min} minute(s).",
                    "Tentativas demais. Tente de novo em {min} minuto(s)."),
    "err.wrongpw": ("Wrong password.", "Senha incorreta."),
    "err.wrongpw.nochange": ("Wrong password. Nothing was changed.",
                             "Senha incorreta. Nada foi alterado."),
    "err.session": ("Not signed in.", "Sessão encerrada."),
    "err.curpw": ("That is not the current password. Nothing was changed.",
                  "Essa não é a senha atual. Nada foi alterado."),
    "err.pwmatch": ("The two new passwords do not match. Nothing was changed.",
                    "As duas senhas novas não coincidem. Nada foi alterado."),
    "err.pwwrite": ("Could not write the new password: {detail}",
                    "Não foi possível gravar a senha nova: {detail}"),
    "err.write": ("Could not write the change: {detail}",
                  "Não foi possível gravar a alteração: {detail}"),
    "err.unknownctl": ("Unknown control '{action}'.",
                       "Controle desconhecido: '{action}'."),
    "err.needspw": ("This change makes a protection weaker, so it needs the panel "
                    "password again.",
                    "Esta alteração deixa uma proteção mais fraca, então pede a senha "
                    "do painel de novo."),
    "ok.pwchanged": ("Password changed.", "Senha alterada."),
    "ok.pwchanged.all": ("Password changed. Every other device has been signed out.",
                         "Senha alterada. Todos os outros dispositivos foram "
                         "desconectados."),
    "pw.blank": ("The password cannot be blank.",
                 "A senha não pode ficar em branco."),
    "pw.short": ("Use at least {n} characters. The password was not changed.",
                 "Use pelo menos {n} caracteres. A senha não foi alterada."),
    "pw.advice.digits": ("This password is digits only, so it is one of the first "
                         "things anyone guesses. The panel locks a device out for 15 "
                         "minutes after 6 wrong tries, which is what is holding the "
                         "line here - and the child has administrator on this PC, so "
                         "he can read the panel's own log.",
                         "Esta senha é só de números, então é uma das primeiras coisas "
                         "que qualquer um tenta. O painel bloqueia um aparelho por 15 "
                         "minutos depois de 6 erros - é isso que está segurando as "
                         "pontas aqui - e a criança tem administrador neste PC, então "
                         "pode ler o próprio log do painel."),
    "pw.advice.short": ("Short passwords lean entirely on the lockout after 6 wrong "
                        "tries. Eight characters or more is a real step up and you "
                        "only type it once per device.",
                        "Senhas curtas dependem inteiramente do bloqueio após 6 "
                        "tentativas erradas. Oito caracteres ou mais é um salto real, "
                        "e você só digita uma vez por aparelho."),

    # --- page chrome (client-side only) ---------------------------------------
    "tab.home": ("Home", "Início"),
    "tab.rules": ("Rules", "Regras"),
    "tab.dns": ("DNS", "DNS"),
    "tab.system": ("System", "Sistema"),
    "b.signout": ("Sign out", "Sair"),
    "b.signin": ("Sign in", "Entrar"),
    "b.apply": ("Apply", "Aplicar"),
    "b.grant": ("Grant", "Conceder"),
    "b.probe": ("Run the probes", "Rodar as sondas"),
    "b.probe.busy": ("Resolving...", "Resolvendo..."),
    "b.fixdns": ("Start it now", "Iniciar agora"),
    "b.fixdns.busy": ("Starting...", "Iniciando..."),
    "b.confirm": ("Confirm", "Confirmar"),
    "b.cancel": ("Cancel", "Cancelar"),
    "b.changepw": ("Change password", "Alterar senha"),
    "h.now": ("Right now", "Agora"),
    "h.now.hint": ("every lane, in one line each", "cada proteção, em uma linha"),
    "n.unknown": ("UNKNOWN is not OFF: it means the reading failed, not that the "
                  "protection is off.",
                  "DESCONHECIDO não é DESLIGADO: significa que a leitura falhou, não "
                  "que a proteção está desligada."),
    "h.resolver": ("Filtering resolver", "Resolvedor de filtragem"),
    "h.resolver.hint": ("the WSL machine every DNS block lives in",
                        "a máquina WSL onde vive todo bloqueio de DNS"),
    "resolver.sub": ("When this is down nothing is filtered <b>and nothing looks "
                     "wrong</b>: the relay falls back to the router, so browsing keeps "
                     "working with no blocking behind it.",
                     "Quando isto cai, nada é filtrado <b>e nada parece errado</b>: o "
                     "relay recorre ao roteador, então a navegação continua funcionando "
                     "sem nenhum bloqueio por trás."),
    "h.gaps": ("Known gaps", "Limitações conhecidas"),
    "h.gaps.hint": ("accepted, not errors - and deliberately not hidden",
                    "aceitas, não erros - e mostradas de propósito"),
    "g.verify": ("verify:", "verifique:"),
    "chip.roblox": ("Roblox", "Roblox"),
    "chip.browser": ("Browsing", "Navegação"),
    "chip.resolver": ("Resolver", "Resolvedor"),
    "chip.dns": ("DNS filtering", "Filtragem DNS"),
    "chip.youtube": ("YouTube", "YouTube"),
    "chip.panel": ("Panel", "Painel"),
    "chip.youtube.v": ("at the resolver, for every device",
                       "no resolvedor, para todos os aparelhos"),
    "h.rules.sub": ("Each protection with its own controls. Changes that make a "
                    "protection <b>weaker</b> ask for the panel password again.",
                    "Cada proteção com seus próprios controles. Alterações que deixam "
                    "uma proteção <b>mais fraca</b> pedem a senha do painel de novo."),
    "k.watchdog": ("Watchdog task", "Tarefa vigia"),
    "k.state": ("State", "Estado"),
    "k.lastrun": ("Last run", "Última execução"),
    "k.lastresult": ("Last result", "Último resultado"),
    "k.nextrun": ("Next run", "Próxima execução"),
    "k.task": ("Task", "Tarefa"),
    "k.lastclose": ("Last close", "Último fechamento"),
    "k.protection": ("Protection", "Proteção"),
    "k.filtering": ("Filtering", "Filtragem"),
    "k.parental": ("Parental control", "Controle parental"),
    "k.safebrowsing": ("Safe Browsing", "Navegação segura"),
    "k.safesearch": ("Safe search", "Busca segura"),
    "k.engines": ("Engines", "Mecanismos"),
    "k.userrules": ("User rules", "Regras do usuário"),
    "k.listsenabled": ("Lists enabled", "Listas ativas"),
    "k.yt.rules": ("User rules (this panel)", "Regras do usuário (este painel)"),
    "k.yt.service": ("Blocked service (watch-time budget)",
                     "Serviço bloqueado (orçamento de tempo)"),
    "det.ytlanes": ("These are two independent lanes. This panel drives the user "
                    "rules; the watch-time budget and <code>toggle youtube</code> "
                    "drive the blocked service. A block from either one holds.",
                    "São duas vias independentes. Este painel controla as regras do "
                    "usuário; o orçamento de tempo e o <code>toggle youtube</code> "
                    "controlam o serviço bloqueado. Um bloqueio de qualquer uma das "
                    "duas vale."),
    "d.why": ("Why this works the way it does", "Por que funciona assim"),
    "c.roblox_limit": ("Per day", "Por dia"),
    "c.roblox_bonus": ("Extra today", "Extra hoje"),
    "c.delay": ("Pause before closing", "Pausa antes de fechar"),
    "c.browser_enabled": ("Guard", "Proteção"),
    "c.unlock": ("Unlock", "Liberar por tempo"),
    "c.browser_limit": ("Per day", "Por dia"),
    "c.notify": ("Tell the child", "Avisar a criança"),
    "c.youtube": ("At the resolver", "No resolvedor"),
    "sw.browser": ("Close any browser that opens",
                   "Fechar qualquer navegador que abrir"),
    "sw.notify": ("Show a message when a browser is closed",
                  "Mostrar uma mensagem quando um navegador for fechado"),
    "hint.roblox_limit": ("0 closes the game within about a minute. The standing "
                          "policy on this PC is 0.",
                          "0 fecha o jogo em cerca de um minuto. A política em vigor "
                          "neste PC é 0."),
    "hint.roblox_bonus": ("For today only - it expires at midnight, so it never "
                          "becomes the new limit.",
                          "Só para hoje - expira à meia-noite, então nunca vira o novo "
                          "limite."),
    "hint.browser_enabled": ("Turning this off has no expiry. For a timed pass, use "
                             "the unlock below.",
                             "Desligar isto não expira sozinho. Para um passe "
                             "temporário, use a liberação abaixo."),
    "hint.unlock": ("Suspends the guard, then it resumes by itself. <b>Unlock before "
                    "reading this panel on the guarded PC</b> - otherwise the guard "
                    "closes the browser you are reading it in, within about a minute.",
                    "Suspende a proteção, que depois volta sozinha. <b>Libere antes de "
                    "abrir este painel no próprio PC vigiado</b> - senão a proteção "
                    "fecha o navegador em que você está lendo, em cerca de um minuto."),
    "hint.browser_limit": ("<b>Stored, not enforced yet.</b> The browser lane has no "
                           "metering today - it closes every browser it finds.",
                           "<b>Gravado, mas ainda não aplicado.</b> A proteção do "
                           "navegador ainda não mede o tempo - ela fecha todo "
                           "navegador que encontrar."),
    "hint.notify": ("Currently <b>off, at the parent's instruction</b>: he sees the "
                    "window close but is not told what closed it. Flip only on "
                    "purpose.",
                    "Hoje fica <b>desligado, por decisão do pai</b>: a criança vê a "
                    "janela fechar mas não fica sabendo o que fechou. Só mude de "
                    "propósito."),
    "hint.delay": ("A random wait between seeing the target and closing it, so the "
                   "moment cannot be timed.",
                   "Uma espera aleatória entre encontrar o alvo e fechá-lo, para que o "
                   "momento não possa ser cronometrado."),
    "hint.youtube": ("The only lane that also covers the Play Games VM, phones and "
                     "TVs; it takes YouTube Music with it. Released on 2026-08-26: "
                     "the mode is \"allowed\" and the repo carries no standing rules, so "
                     "nothing puts the block back on its own. Blocking here - by hand or "
                     "with a schedule - holds until you change it.",
                     "A única via que também alcança a VM do Play Games, celulares e "
                     "TVs; leva o YouTube Music junto. Liberado em 26/08/2026: o modo "
                     "é \"liberado\" e o repositório não tem mais regras permanentes, "
                     "então nada recoloca o bloqueio sozinho. Bloquear aqui - à mão ou "
                     "com um horário - vale até você mudar."),
    "lbl.soonest": ("soonest", "mínimo"),
    "lbl.latest": ("latest", "máximo"),
    "h.dns": ("DNS filtering", "Filtragem DNS"),
    "h.probes": ("Live verification", "Verificação ao vivo"),
    "h.probes.hint": ("resolves real names, reads AdGuard's verdict",
                      "resolve nomes reais e lê o veredito do AdGuard"),
    "det.probes": ("The verdict is AdGuard's query-log <code>reason</code>, never the "
                   "address that came back: a parental block answers with "
                   "<code>94.140.14.35</code>, AdGuard's block page, so judging by IP "
                   "reports a working block as a leak.",
                   "O veredito é o <code>reason</code> do log de consultas do AdGuard, "
                   "nunca o endereço que voltou: um bloqueio parental responde com "
                   "<code>94.140.14.35</code>, a página de bloqueio do AdGuard, então "
                   "julgar pelo IP faz um bloqueio funcionando parecer um vazamento."),
    "probe.notrun": ("Not run yet in this session.", "Ainda não rodou nesta sessão."),
    "probe.when": ("last run {at} against {server}",
                   "última execução {at} contra {server}"),
    "t.list": ("Filter list", "Lista de filtros"),
    "t.state": ("State", "Estado"),
    "t.rules": ("Rules", "Regras"),
    "t.updated": ("Updated", "Atualização"),
    "t.probe": ("Probe", "Sonda"),
    "t.domain": ("Domain", "Domínio"),
    "t.expected": ("Expected", "Esperado"),
    "t.verdict": ("Verdict", "Veredito"),
    "t.detail": ("Detail", "Detalhe"),
    "t.task": ("Task", "Tarefa"),
    "t.policy": ("Policy", "Política"),
    "t.value": ("Value", "Valor"),
    "t.wanted": ("Wanted", "Esperado"),
    "t.empty": ("Nothing to show.", "Nada para mostrar."),
    "h.tasks": ("Scheduled tasks", "Tarefas agendadas"),
    "h.tasks.hint": ("every SafeHouse task, including the missing ones",
                     "todas as tarefas SafeHouse, inclusive as que faltam"),
    "h.policy": ("Browser policy", "Política de navegador"),
    "h.policy.hint": ("machine policy in HKLM, out of reach of browser settings",
                      "política de máquina no HKLM, fora do alcance das configurações "
                      "do navegador"),
    "h.access": ("Panel access", "Acesso ao painel"),
    "access.sub": ("Changing the password signs every other device out, including any "
                   "phone still holding a session. It needs the current password, so a "
                   "session left open on an unlocked phone cannot lock you out of your "
                   "own controls.",
                   "Alterar a senha desconecta todos os outros dispositivos, inclusive "
                   "um celular que ainda esteja com sessão aberta. Ela pede a senha "
                   "atual, então uma sessão esquecida aberta num celular desbloqueado "
                   "não pode trancar você para fora dos seus próprios controles."),
    "f.current": ("Current password", "Senha atual"),
    "f.new": ("New password", "Senha nova"),
    "f.again": ("New password again", "Senha nova de novo"),
    "m.title": ("This change weakens a protection",
                "Esta alteração enfraquece uma proteção"),
    "m.ph": ("Panel password", "Senha do painel"),
    "toast.cancel": ("Cancelled - nothing was changed.",
                     "Cancelado - nada foi alterado."),
    "toast.failed": ("Failed ({s}).", "Falhou ({s})."),
    "err.probes": ("Probes failed.", "As sondas falharam."),
    "hm.host": ("{m} · listening on {a}", "{m} · atendendo em {a}"),
    "hm.updated": ("updated {t}", "atualizado {t}"),
    "foot": ("Ranges are enforced on the server as well as here. Every change is "
             "written to {path} with the time and the address it came from.",
             "Os limites valem no servidor, não só aqui. Toda alteração é gravada em "
             "{path} com a hora e o endereço de onde veio."),
    "k.pihole": ("Pi-hole ({server})", "Pi-hole ({server})"),

    # login page
    "login.sub": ("Parental controls for this PC. Sign in to see and change what is "
                  "enforced.",
                  "Controles parentais deste PC. Entre para ver e alterar o que está "
                  "em vigor."),
    "login.ph": ("Panel password", "Senha do painel"),
    "login.http": ("This connection is not encrypted (no HTTPS). Use it on the house "
                   "network only.",
                   "Esta conexão não é criptografada (sem HTTPS). Use apenas na rede "
                   "da casa."),
    "login.failed": ("Sign-in failed ({s}).", "Falha ao entrar ({s})."),

    # first-run setup page
    "setup.title": ("SafeHouse control panel", "Painel de controle SafeHouse"),
    "setup.nopw": ("No password has been set yet, so the controls are not available.",
                   "Nenhuma senha foi definida ainda, então os controles não estão "
                   "disponíveis."),
    "setup.elev": ("On the machine itself, in an <b>elevated</b> prompt:",
                   "Na própria máquina, em um prompt <b>elevado</b> (como "
                   "administrador):"),
    "setup.writes": ("That writes the new password to "
                     "<code>C:\\ProgramData\\SafeHouse\\panel-password.txt</code>, "
                     "which only Administrators can read. Store it, then delete that "
                     "file.",
                     "Isso grava a senha nova em "
                     "<code>C:\\ProgramData\\SafeHouse\\panel-password.txt</code>, que "
                     "só Administradores conseguem ler. Guarde-a e depois apague esse "
                     "arquivo."),
    "setup.hash": ("The panel keeps only a hash. Nothing on this page can set the "
                   "password - if it could, whoever opened this page first would own "
                   "the panel. Once you are signed in you can change it under <b>Panel "
                   "access</b>, which asks for the current one.",
                   "O painel guarda apenas um hash. Nada nesta página consegue definir "
                   "a senha - se conseguisse, quem abrisse esta página primeiro seria "
                   "dono do painel. Depois de entrar, você pode alterá-la em <b>Acesso "
                   "ao painel</b>, que pede a senha atual."),

    # --- the per-game Roblox lane -------------------------------------------
    # The vocabulary here is deliberately NOT the vocabulary of the budget card. The
    # parent has to be able to tell "no time left" from "not this game" at a glance on a
    # phone, and two cards both saying "Roblox / blocked" would make that impossible.
    "lane.games": ("Roblox games", "Jogos do Roblox"),
    "lane.how.games": ("reads the game's own log and ends Roblox when the experience "
                       "is not on the list",
                       "lê o log do próprio jogo e encerra o Roblox quando a "
                       "experiência não está na lista"),
    "games.v.allow": ("only the listed games", "só os jogos da lista"),
    "games.v.deny": ("all games except the listed ones",
                     "todos os jogos, menos os da lista"),
    "games.reason.off": ("the game filter is off - any experience may run",
                         "o filtro de jogos está desligado - qualquer experiência pode "
                         "rodar"),
    "games.reason.emptyallow": ("the list is empty and the mode is \"only the listed "
                                "games\", so EVERY experience will be ended",
                                "a lista está vazia e o modo é \"só os jogos da "
                                "lista\", então TODA experiência será encerrada"),
    "games.note.emptyallow": ("Add at least one game, or switch the mode - as it "
                              "stands, Roblox closes on every join.",
                              "Adicione pelo menos um jogo, ou troque o modo - do "
                              "jeito que está, o Roblox fecha em toda entrada."),
    "games.note.off": ("Nothing here is enforced while the filter is off. The daily "
                       "time budget is a separate lane and still applies.",
                       "Nada aqui vale enquanto o filtro está desligado. O limite "
                       "diário de tempo é outra proteção e continua valendo."),
    "games.note.samegame": ("A game can have several maps. The list is keyed on the "
                            "GAME, so moving between maps of an allowed game is fine.",
                            "Um jogo pode ter vários mapas. A lista é por JOGO, então "
                            "andar entre mapas de um jogo liberado não é problema."),
    "fact.games_mode": ("Mode", "Modo"),
    "fact.games_listed": ("Games on the list", "Jogos na lista"),
    "fact.games_seen": ("Games seen so far", "Jogos vistos até agora"),
    "field.games_enabled": ("game filter", "filtro de jogos"),
    "ctl.games.on": ("The game filter is ON.", "O filtro de jogos está LIGADO."),
    "ctl.games.off": ("The game filter is OFF - any Roblox experience may run.",
                      "O filtro de jogos está DESLIGADO - qualquer experiência do "
                      "Roblox pode rodar."),
    "ctl.games.mode": ("Mode is now \"{mode}\" (was \"{was}\").",
                       "O modo agora é \"{mode}\" (era \"{was}\")."),
    "ctl.games.list": ("The list now holds {n} game(s).",
                       "A lista agora tem {n} jogo(s)."),
    "ctl.games.added": ("Added: {ids}.", "Adicionados: {ids}."),
    "ctl.games.removed": ("Removed: {ids}.", "Removidos: {ids}."),
    "err.games_mode": ("The mode must be \"allow\" or \"deny\", not {got}.",
                       "O modo tem de ser \"allow\" ou \"deny\", não {got}."),
    "err.games_list": ("The list of games must be a list of universe ids.",
                       "A lista de jogos tem de ser uma lista de ids de universo."),
    "err.games_toomany": ("{n} games is more than the {max} this panel accepts.",
                          "{n} jogos é mais do que os {max} que este painel aceita."),
    "err.games_id": ("{got} is not a universe id. Ids are whole numbers above zero.",
                     "{got} não é um id de universo. Ids são números inteiros maiores "
                     "que zero."),
    "chip.games": ("Roblox games", "Jogos do Roblox"),
    "c.games_enabled": ("Game filter", "Filtro de jogos"),
    "c.games_mode": ("Mode", "Modo"),
    "c.games_list": ("The list", "A lista"),
    "sw.games": ("Only let the agreed games run",
                 "Deixar rodar só os jogos combinados"),
    "hint.games_enabled": ("Separate from the daily time budget. This one decides "
                           "WHICH games, that one decides FOR HOW LONG - and both have "
                           "to pass.",
                           "Separado do limite diário de tempo. Este decide QUAIS "
                           "jogos, aquele decide POR QUANTO TEMPO - e os dois têm de "
                           "aprovar."),
    "hint.games_mode": ("\"Only the listed games\" is the tight one and the right "
                        "default for a young child. \"All except\" only helps against "
                        "a game you already know the name of.",
                        "\"Só os da lista\" é o modo apertado e o certo para criança "
                        "pequena. \"Todos menos\" só ajuda contra um jogo que você já "
                        "sabe o nome."),
    "hint.games_list": ("The games below are the ones actually opened on this PC, "
                        "newest first. Ticking one takes effect within seconds - "
                        "Roblox closes the moment a game that is not allowed loads, so "
                        "he sees the loading screen and then the desktop.",
                        "Os jogos abaixo são os que realmente foram abertos neste PC, "
                        "do mais recente para o mais antigo. Marcar um vale em poucos "
                        "segundos - o Roblox fecha assim que carrega um jogo que não "
                        "está liberado, então ele vê a tela de carregamento e depois a "
                        "área de trabalho."),
    "games.empty": ("No games recorded yet. They appear here the first time each one "
                    "is opened.",
                    "Nenhum jogo registrado ainda. Eles aparecem aqui na primeira vez "
                    "que cada um for aberto."),
    "games.unidentified": ("the game's log did not say which experience this was",
                           "o log do jogo não disse qual experiência era esta"),
    "games.joins": ("{n} time(s)", "{n} vez(es)"),
    "games.save": ("Save the list", "Salvar a lista"),
    "games.mat.minimal": ("Minimal", "Mínimo"),
    "games.mat.mild": ("Mild", "Leve"),
    "games.mat.moderate": ("Moderate", "Moderado"),
    "games.mat.restricted": ("Restricted", "Restrito"),
    "games.mat.unknown": ("not rated", "sem classificação"),
    "games.note.ceiling": ("Roblox rates each experience. Anything above the ceiling is "
                           "marked here and still runs if you ticked it - your list "
                           "wins. Measured on 2026-08-30: <b>7 of the 15 games already "
                           "played are rated Mild</b>, above the account's Minimal "
                           "setting, and Roblox let them start anyway.",
                           "O Roblox classifica cada experiência. O que passa do teto "
                           "fica marcado aqui e continua rodando se você marcou - sua "
                           "lista manda. Medido em 30/08/2026: <b>7 dos 15 jogos já "
                           "jogados são classificados como Leve</b>, acima do ajuste "
                           "Mínimo da conta, e o Roblox deixou abrir mesmo assim.")
}

CATALOG: dict[str, dict[str, str]] = {
    "en": {key: pair[0] for key, pair in _STRINGS.items()},
    "pt": {key: pair[1] for key, pair in _STRINGS.items()},
}
