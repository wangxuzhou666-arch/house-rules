[English](WORKSHOP.md) | 中文

# 工作坊：从一条 review 发现到一条规则，以及这条规则管不管用

house-rules 把你的编码规范放在一个规则文件里，编译成一份简短的 digest，供你的编码会话和代码 reviewer 一起读，并记录 reviewer 发现了什么、每条发现后来怎么处理了。它适合用 Claude Code（它的 `/code-review`）的人，或者能把别的 reviewer 的输出以 JSON 带进来的人。

带你亲手把 house-rules 走一遍，分六个小节，总共 45 到 60 分钟。你会读一份规则文件，回放一次已记录的 review，把 review 暴露的缺口变成一条规则，看一眼这个项目里唯一的测量，把自动化和它的限额配起来，再试着改进一条规则。

主线是免费、可复现的：回放的是录好的记录，不调用任何模型。可能花钱的两步（第 5 节一步，第 6 节一步）会在你运行之前先说明，而且都可以跳过。

这个工作坊不会给你的，是“用了这些规则之后 agent 写出更好的代码”的证据。项目本身也不这么主张。它给你的是一套办法：把 reviewer 反复发现的问题写下来，逐条决定哪些变成规则，再检查某条规则有没有改变 reviewer 的行为。第 4、6 节会让你看到最后这个检查有多难。

一个人做或一群人做都行；每一节末尾有一个“检查点”，用自己的话说出来。第 4、6 节另有讨论题。命令本身几秒就跑完，时间花在读输出和做练习上。“换成你自己的”这些框是可选的加餐，不计入各节的时间。

链接到的参考文档，只有链接文字后带“（中文）”的才有中文版，其余都只有英文。

## 开始之前

你需要 `git` 2.28 或更新版本（并已设置姓名和邮箱，因为你会提交）、Python 3.10 或更新版本，以及一个 POSIX shell（macOS 或 Linux 上的 bash 或 zsh，Windows 上用 WSL：命令里用到了 `sed -i.bak`、`seq` 和 heredoc）。这个工具只用 Python 标准库。免费的这条路不需要 Claude Code。

```sh
python3 --version
git clone https://github.com/wangxuzhou666-arch/house-rules
cd house-rules
export HR_SRC="$PWD"
export PATH="$HR_SRC/bin:$PATH"
house-rules --help
```

- 要 clone，不要下载 zip。第 4 节需要 git 仓库，没有的话会直接报错停下。
- `python3 --version` 必须打印 3.10 或更新的版本，因为 `house-rules` 用你 `PATH` 里的第一个 `python3` 运行。如果版本更低，`house-rules --help` 会只打印一行就停下：`house-rules: error: Python 3.10 or newer is required, but this is Python <version> (<path of that python3>)`。然后请在这个终端里把较新的 Python 放到 `PATH` 最前面，再运行一次。
- 整个工作坊用同一个终端，因为那两行 `export`，以及为较新的 Python 改的 `PATH`，只在这个终端里有效。
- `house-rules --help` 会列出大约二十个子命令。工作坊用到其中十个，每个都在第一次出现的地方介绍：`init`、`compile`、`review`、`count`、`decision-context`、`prepare-decisions`、`apply-decisions`、`decision-summary`、`wrapup` 和 `label`。
- 第 1、2、3、5 节在你 clone 旁边的一个临时仓库 `hr-demo` 里做，而且一节接着一节。第 4、6 节直接在 clone 里做，第 6 节结尾会撤销改动。想把某一节重来，就删掉 `hr-demo`（`rm -rf "$HR_SRC/../hr-demo"`），从第 1 节重做：已经存在的目录会让 `mkdir` 那几行报错。
- 下面的输出块是某一次运行记录下来的。你那边的哈希、日期、路径和个别字节数会不一样，形状应该一样。

全文用到的词：

| 词 | 在这里的含义 |
|---|---|
| 规则文件 | 你的编码规范，写成带稳定 ID 的条款（比如 `ERR.1`）：`CODING-STYLE.md` |
| 条款（clause） | 规则文件里的一条规则：一个 ID、正文和一个严重程度，比如 `**[ERR.1]** Propagate configuration parsing errors. (should-fix)` |
| 严重程度（severity） | 违反的严重程度：`blocker`、`should-fix` 或 `nice` |
| digest | 规则文件的精简版，编译成 `CODING-STYLE.digest.md`。编码会话和 reviewer 都会读它，所以它有字节预算 |
| reviewer | 不属于这个项目的一次代码 review：Claude Code 内置的 `/code-review`，或者任何一份你导入的 review 输出 |
| 发现（finding） | reviewer 报告的一个缺陷，带文件、行号和标签（条款 ID，或 `unmapped`） |
| 盲点 | 没有任何条款覆盖的真实缺陷，所以它的标签是 `unmapped` |
| ledger | 本地文件 `.style-findings.jsonl`，记录发现和它们的处置结果。它被 git 忽略 |
| hook | git（或 Claude Code）在固定时刻运行的一段脚本。`init` 装两个 git hook，一个在提交前、一个在提交后；第 5 节还会用到 Claude Code 的 Stop hook，它在一轮对话结束时运行 |
| wrap-up | 对上次 review 以来的工作做的一次 review，只有改动量够大时才启动（第 5 节）。`init --no-wrapup-auto` 的意思是：不让任何一次提交自己启动它 |
| 轮（round） | 对一个改动做的一次 review。一个改动最多 3 轮。在默认分支上，每个新提交都是一个新的改动，所以它的轮数从 0 重新开始（第 2、5 节） |
| run ID | 你给一次 review 起的名字（`--run live-r3`）。再次导入同一个 ID 是回放：不记录任何新东西，也不占一轮 |
| 回放（replay） | 重新读一份已记录的 review，而不是调用模型：免费，而且每次都一样 |
| 用例（case） | 从第 4 节起：一条规则、一个违反它的文件和一个对照 |
| 对照（control） | 一个看起来相似、但是合法的文件，不应该被报出来。它要么符合规则（`compliant`），要么属于规则明文豁免的情形（`exempt`） |
| oracle | 一个脚本：让违规文件和对照在注入的故障下运行，根据行为判断哪个坏了。它不调用模型 |
| 留出的（held-out） | 在第 1 轮之后才写的用例，规则措辞改变时也不拿它当证据，所以是对这条规则更公平的检验（`rel-1b`、`rel-2b`） |

---

## 1. 规则文件就是你的标尺（约 8 分钟）

**目标。** 认识后面一切所依赖的三样东西：带条款 ID 的规则文件、由它编译出的 digest，以及 digest 的字节预算。亲眼看 pre-commit hook 拒绝一次 digest 过期的提交。

**命令。**

```sh
cd "$HR_SRC"
# 如果 ../hr-demo 已经存在，mkdir 会失败：想重来，先 rm -rf ../hr-demo
mkdir ../hr-demo && cd ../hr-demo && git init -q -b main
# 去掉 REL.1 的样例规则：第 6 到 9 行是 REL.1 和它的说明，留下 ERR.1 和 R.1
sed '6,9d' "$HR_SRC/samples/config-replacement/CODING-STYLE.md" > CODING-STYLE.md
cat CODING-STYLE.md
house-rules init --dry-run --no-wrapup-auto --rules-file CODING-STYLE.md
house-rules init --yes --no-wrapup-auto --rules-file CODING-STYLE.md
house-rules compile --check --strict
wc -c CODING-STYLE.digest.md
git add .house-rules.json CODING-STYLE.md CODING-STYLE.digest.md CLAUDE.md .gitignore
git commit -q -m "chore: adopt house-rules"
```

那一块里有三样是新的。`--rules-file` 指明哪个文件放着你的条款。`--no-wrapup-auto` 让自动 wrap-up 保持关闭，所以工作坊里你提交的任何东西都不会自己启动一次 review（第 5 节会解释）。`--strict` 让 `compile --check` 在 digest 超出字节预算时也退出 1，而不只是在它过期时。

这份语料是两条条款加一句说明：

```text
# Sample project rules

This is a small teaching corpus, not a required style for other repositories.

- **[ERR.1]** Propagate configuration parsing errors; do not silently substitute a successful-looking default. (should-fix)
- **[R.1]** Review correctness as well as these rules. Use unmapped for a real defect no code rule covers.
```

`--dry-run` 会打印 `init` 将要做的事，每个文件一份 diff（总共约 100 行：扫一眼就行），什么都不写。真正运行时会写入配置、digest、`CLAUDE.md` 里对 digest 的 `@import`（一行让 Claude Code 去加载那个文件的写法，这样 Claude Code 和 `/code-review` 都会读到它）、两个 git hook 和 `.gitignore` 条目。然后读一下 digest：

```sh
cat CODING-STYLE.digest.md
```

现在试一试这道保险。改一下规则文件，只暂存这处改动，然后提交：

```sh
sed -i.bak 's/small teaching corpus/small teaching corpus for the workshop/' CODING-STYLE.md && rm CODING-STYLE.md.bak
git add CODING-STYLE.md
git commit -m "docs: say what the corpus is for"
```

这次提交会被拒绝。重新生成 digest，暂存它，再提交一次：

```sh
house-rules compile
git add CODING-STYLE.digest.md
git commit -m "docs: say what the corpus is for"
```

**你会看到。**

```text
  config           added        .house-rules.json: rules_files ['CODING-STYLE.md'], wrapup_auto false
  compile          added        digest CODING-STYLE.digest.md: 925 bytes
  CLAUDE.md        added        @CODING-STYLE.digest.md under `## Code style digest (house-rules)`, in a new CLAUDE.md ...
  hook pre-commit  added        .git/hooks/pre-commit (runs with <python>); live check: the block ran
  hook post-commit added        .git/hooks/post-commit (runs with <python>); live check: the block ran
  .gitignore       added        .style-findings.jsonl, .house-rules-label-page.html, .house-rules-label-batch.json, .house-rules/
  start point      skipped      no commit yet
compile --check: 1 output(s) up to date
     925 CODING-STYLE.digest.md
```

`init` 还会打印一行现成的 `git add ... && git commit`（上面的命令做的是同一件事）和三条 `next` 提示。digest 里每条条款一行（`ERR.1 | should-fix | Propagate configuration parsing errors`），每条 reviewer 准则一行，外加自动生成的 Labeling 段，它要求 reviewer 用 `category`（kebab-case 的条款 ID，或 `unmapped`）和 `severity` 来回答。被拒绝的那次提交会打印：

```text
stale: CODING-STYLE.digest.md (digest, the STAGED version is out of date)
this check reads what is staged, not the working tree: run `house-rules compile` if the working tree is stale too, then `git add CODING-STYLE.digest.md`
```

**换成你自己的。** 要用你自己的规则文件，就在你的仓库里让 `init` 指向它：`house-rules init --rules-file docs/CODING-STYLE.md`。想先练习，就在一个单独的目录里用样例的临时副本，这样 `hr-demo` 不会被动到。条款的形状是这样的：

```text
- **[ID]** What must hold. (should-fix) <!-- digest: a shorter text for the digest -->
```

建一个临时仓库（`init` 需要一个），从样例规则开始，再加一条你自己的长条款：

```sh
cd "$HR_SRC/.."
mkdir hr-mine && cd hr-mine && git init -q -b main
cp "$HR_SRC/samples/config-replacement/CODING-STYLE.md" CODING-STYLE.md
cat >> CODING-STYLE.md <<'EOF'
- **[RET.1]** Every public function that can fail must document the exception it raises and the conditions that trigger it, in its docstring. (should-fix)
EOF
house-rules init --yes --no-wrapup-auto --rules-file CODING-STYLE.md
grep '^RET.1' CODING-STYLE.digest.md
```

digest 里的那一行，是条款正文在第一个 `;` 或句末的 `.` 处截断，并且最多 90 个字符，所以长条款会以 `…` 结尾：

```text
RET.1 | should-fix | Every public function that can fail must document the exception it raises and the conditi…
```

现在给它写一段你自己的短文本，也就是 `digest:` 注释；检查 digest 已经过期，重新生成，再读一遍那一行：

```sh
sed -i.bak 's/in its docstring. (should-fix)$/in its docstring. (should-fix) <!-- digest: Document the exception a public function raises -->/' CODING-STYLE.md && rm CODING-STYLE.md.bak
house-rules compile --check --strict
house-rules compile
grep '^RET.1' CODING-STYLE.digest.md
```

`compile --check --strict` 不会打印 digest 的行；它说 `stale: CODING-STYLE.digest.md (digest, out of date)` 并退出 1，因为你改了规则、没改 digest。`compile` 会重写它，这时 `grep` 显示 `RET.1 | should-fix | Document the exception a public function raises`。想亲眼看预算怎么卡住你，就把它调低再检查一次：

```sh
cat > .house-rules.json <<'EOF'
{
  "rules_files": ["CODING-STYLE.md"],
  "wrapup_auto": false,
  "digest_max_bytes": 600
}
EOF
house-rules compile --check --strict
```

它会打印 `warning: digest CODING-STYLE.digest.md is 1132 bytes, over digest_max_bytes 600` 并退出 1（不带 `--strict` 则退出 0）。digest 会被载入每一个会话，所以它有上限（默认 4096 字节），也所以一条长条款需要一段简短的 `digest:` 文本。把预算改回来（删掉 `digest_max_bytes` 那一行），然后在 `CODING-STYLE.md` 里用你自己的三条条款替换 `RET.1`，写上 `digest:` 文本，再编译一次。

**检查点。** 你能说出：（1）digest 的一行里有什么；（2）digest 为什么有字节预算；（3）pre-commit hook 拒绝什么，以及它为什么检查的是暂存区里的那一份。

**延伸阅读。** [Your CODING-STYLE.md](docs/CUSTOMIZATION.md#your-coding-stylemd) · [措辞与 digest 预算（中文）](docs/RULE-WRITING.zh.md#措辞与-digest-预算) · [Compile reference](docs/COMPILE.md) · [`init` 改了什么、怎么撤销](docs/ONBOARDING.md)

---

## 2. 用回放认识 reviewer（约 10 分钟）

**目标。** 不调用模型，看着一次真实的 review 落进 ledger。这份记录是一次真实 `/code-review` 的原始输出，对象是三个带已知缺陷的小文件。你把它导入进来，就当是刚跑完一样，并弄清什么是一条发现、`unmapped` 是什么意思、“回放，不是新的一轮”是什么意思。

这份记录是在规则里已经有 REL.1 的时候录的。你的规则里还没有它，这正是重点：同一个缺陷现在看起来是个盲点。

**命令。**

```sh
cd "$HR_SRC/../hr-demo"
# 那次 review 看到的三个文件是 reviewed/ 里的副本；samples/config-replacement/ 顶层同名的文件（safe_config.py、heldout_safe.py）更新过：这里不要用
cp "$HR_SRC"/samples/config-replacement/reviewed/*.py .
cat heldout_unsafe.py
house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run live-r3
house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run live-r3
house-rules count --by-rule --label-source
```

`--from-file` 是导入一份已经存在的 review，而不是去运行一次；`--run live-r3` 是 run ID，也就是这次 review 的名字。第二次导入是故意的：同一个文件、同一个 run ID。

**你会看到。** `heldout_unsafe.py` 先以写模式打开设置文件，这会把它清空，然后才序列化数据。第一次导入：

```text
review round 1/3 for main@<12 hex>
saved raw review output to .house-rules/reviews/live-r3.txt
id         rule_id   severity    location             summary
live-r3-1  unmapped  should-fix  heldout_unsafe.py:7  path.open("w") truncates the existing settings file before…
live-r3-2  unmapped  nice        config_store.py:18   The replacement is written to a fresh temp file and os.repl…
review: +2 finding(s) from 2 item(s) (0 merged by file+line, 2 unmapped, 0 labeled from the text, 0 without a usable severity), 0 already recorded
open: 2. A commit with a `Fixes-Finding: <id>` trailer closes a finding.
```

第二次导入什么也不记录：`review: replay of run live-r3 (already recorded; not another round)` 和 `no new findings`。然后是 `count`：

```text
findings: 2 total, 2 open (gate 0 / l1 2 / l2 0); events: 3
  unmapped: 2 (model 2)
review rounds on main@<12 hex>: 1/3
```

最后一行是轮数计数器：这个改动可以有的 3 轮里，用掉了 1 轮。这里的“改动”指的是 `main` 在当前这个提交的状态；`main` 上再来一个新提交，就会开始新的计数（第 5 节会演示）。

发现 1 是一个真实的“先删后写”缺陷，你的两条规则都没覆盖它。记录里写的是 `"category": "rel-1"`，而你的语料里没有这条条款，导入器解析不了这个标签，于是把它记成 `unmapped`：一个盲点。发现 2（替换用的新文件丢了旧文件的权限位）是另一个更小的 bug。

**换成你自己的。** 导入你自己的一份 review。把任意 reviewer 的输出存成 JSON 数组，每个对象带 `file`、`line`、`summary`、`failure_scenario`、`category`（kebab-case 的条款 ID，或 `unmapped`）和 `severity`（`blocker`、`should-fix` 或 `nice`），然后在你自己的仓库里导入，或者在 `hr-mine` 里导入，也就是第 1 节“换成你自己的”建的那个临时仓库（`cd "$HR_SRC/../hr-mine"`）。不要在 `hr-demo` 里导入：第 3 节的帮手只认得那两条已记录的发现，多出第三条它就会报一串错然后停下。

```bash
house-rules review --from-file mine.json --run mine-1
```

字段名不同的话，需要你自己写适配；没有字段映射的设置项。示例对象见 [Your review output](docs/CUSTOMIZATION.md#your-review-output)。

**检查点。** 你能解释：为什么第二次导入不花钱也不占一轮；`unmapped` 是什么意思，以及 digest 为什么告诉 reviewer 宁可标 `unmapped`，也不要硬把问题套进某条条款；一个改动最多 review 3 轮（你用了 1 轮）。

**延伸阅读。** [Review rounds](docs/LEDGER.md#review-rounds) · [Rule ids](docs/LEDGER.md#rule-ids) · [Review output from elsewhere](docs/INTEGRATION.md#review-output-from-elsewhere) · [样例的 README](samples/config-replacement/README.md)

---

## 3. 盲点变成规则（约 10 分钟）

**目标。** 把“决定”这一步从头走到尾：证据、评估、提案、一条被记录下来的人的回答，然后是规则改动、新的 digest 和一条 changelog。

在 Claude Code 里，评估由模型写，由原生弹窗来问你（`/house-rules:maintain`）。这里不调用模型，所以由一个小帮手 [`make_answers.py`](samples/config-replacement/make_answers.py) 扮演两个角色，并把每个仓库都不一样的 ID 填好。其余一切都是真的工具。这个帮手对发现 1 提议新增一条条款 REL.1，对发现 2 提议“保留，不立规则”，并给出的回答是接受和暂缓。

**命令。**

```sh
cd "$HR_SRC/../hr-demo"
mkdir -p .house-rules/work
house-rules decision-context > .house-rules/work/context.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" assessment .house-rules/work/context.json > .house-rules/work/assessment.json
house-rules decision-context --assessment .house-rules/work/assessment.json > .house-rules/work/cards.json
house-rules prepare-decisions --assessment .house-rules/work/assessment.json > .house-rules/work/prepared.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" answers .house-rules/work/prepared.json > .house-rules/work/answers.json
house-rules apply-decisions --dry-run --input .house-rules/work/answers.json
```

工具只从仓库内部读取这些文件，而 `.house-rules/` 被 git 忽略，所以这些临时文件既不会被提交，也不会在第 5 节里被算作工作量。打开 `assessment.json`（模型说了什么）、`cards.json`（人会看到什么：发现、代码、确切的改动前后文本）和 `answers.json`（人说了什么）。到这里什么都还没改，所以现在正好可以试两件事；两件都是可选的，而且都只能在下面真正应用之前做。

**换成你自己的。** 先试试换一个回答。在回答文件的副本里把 `accept` 改成 `revise`，再试运行：

```bash
sed 's/"decision": "accept"/"decision": "revise"/' .house-rules/work/answers.json > .house-rules/work/answers-revise.json
house-rules apply-decisions --dry-run --input .house-rules/work/answers-revise.json
```

`changed` 会是空的，因为工具只记录“请修改”的请求，什么也不应用（它的计数器把这类回答记在 `rejected` 下）。然后让这条条款变成你的：改它的措辞，检查，再准备一个新的批次：

```bash
sed 's/Preserve the existing usable configuration until/Keep the old configuration until/' .house-rules/work/assessment.json > .house-rules/work/assessment-b.json
house-rules decision-context --assessment .house-rules/work/assessment-b.json > .house-rules/work/cards-b.json
house-rules prepare-decisions --assessment .house-rules/work/assessment-b.json > .house-rules/work/prepared-b.json
python3 "$HR_SRC/samples/config-replacement/make_answers.py" answers .house-rules/work/prepared-b.json --confirmed-by "Your Name" > .house-rules/work/answers-b.json
house-rules apply-decisions --dry-run --input .house-rules/work/answers-b.json
```

如果你做了这一步，就在下一块里应用 `answers-b.json`，而不是 `answers.json`。这时你的措辞会出现在 `CODING-STYLE.md` 和 changelog 里，changelog 写的是 `accept by Your Name`；digest 里的那一行和它的 1063 字节不会变，因为这条条款自带 `digest:` 文本；而 `decision-summary` 还会把你准备了却从没回答的第一个批次记为 `pending`。这两件事都不要在真正应用之后再试：那时 REL.1 已经存在，证据变了，工具会以 `assessment context changed; collect and assess again` 停下。想再试一次，就在一个全新的 `hr-demo` 里重做第 3 节。

现在应用这些回答，检查结果并提交：

```sh
house-rules apply-decisions --input .house-rules/work/answers.json
house-rules compile --check --strict
house-rules decision-summary
git diff --stat
wc -c CODING-STYLE.digest.md
git add CODING-STYLE.md CODING-STYLE.digest.md CODING-STYLE.changelog.md
git commit -q -m "rules: add REL.1"
```

**你会看到。** 试运行和真正应用打印同一个对象，`"status"` 先是 `"preview"`，然后是 `"applied"`：

```text
{
  "accepted": 1,
  "answer_counts": {
    "accept": 1,
    "defer": 1
  },
  "batch_id": "<64 hex>",
  "changed": [
    "CODING-STYLE.md",
    "CODING-STYLE.digest.md",
    ".house-rules/proposals/version.json",
    "CODING-STYLE.changelog.md"
  ],
  "rejected": 0,
  "status": "applied"
}
```

`compile --check` 退出码为 0，`decision-summary` 报告一条被接受的改动和一条被暂缓的无改动，`git diff --stat` 显示 `CODING-STYLE.md | 3 +++` 和 `CODING-STYLE.digest.md | 3 ++-`。digest 从 925 字节涨到 1063 字节，多了一行 `REL.1 | should-fix | Preserve usable configuration until ...`。`CODING-STYLE.changelog.md` 是新文件：确切的条款文本、理由、证据（发现 ID、`heldout_unsafe.py:7`、代码片段），以及用原话记下的决定：`Decision: accept by workshop learner — "Yes, add REL.1 as written."`

工具有两条规矩值得你亲眼看看。没有明确回答的那一组永远不会被应用：默认不等于同意。而且接受一条规则，并不会把它来源的那条发现标成已修复；关闭发现是另一个动作（第 5 节）。

真正使用时，由 `/house-rules:maintain` 扮演模型的角色，并通过宿主的原生提问工具来问你（需要 Claude Code；不在这条免费路径里）。

**检查点。** 你能说清谁做了什么：工具保存证据并校验；模型提案；人来决定；没有 `accept`，什么都不会改。你能在 `CODING-STYLE.changelog.md` 里找到确切的条款文本和那条决定。

**延伸阅读。** [Reusable assessment prompt](docs/DECISION-CARDS.md#reusable-assessment-prompt) · [Native human decisions](docs/DECISION-CARDS.md#native-human-decisions) · [编写可执行、可追溯的规则（中文）](docs/RULE-WRITING.zh.md) · [样例的 maintenance prompt](samples/config-replacement/maintenance-prompt.md)

---

## 4. 你的判断能泛化吗？（约 10 分钟）

**目标。** 认识这个项目里唯一的测量，并且读它时不读过头。问题是：一条规则从某条发现里学来之后，reviewer 遇到同一种机制的新实例时，会不会报出来、贴上这条规则的 ID，并放过一个合法的相似写法？这里的一切都是回放已记录的 review：免费、可复现、不调用模型。

**命令。**

```sh
cd "$HR_SRC"
python3 -I samples/generalization/cases/rel-1/oracle.py
for f in samples/generalization/cases/rel-1/code/*.py; do echo "== $f"; cat "$f"; done
python3 samples/generalization/run_check.py --round 1
python3 samples/generalization/run_check.py
git status --short
```

oracle 会让两个文件在注入的故障下运行，每个事实打印一行。什么是真的，由行为决定，而不是由模型决定。（`-I` 是 Python 的隔离模式：它忽略 `PYTHON*` 环境变量和用户的 site-packages，所以除标准库之外没有任何东西能改变 oracle 做的事。）这两个文件带着各自的名字依次打印出来，先是对照（`preview_cache.py`，一个按需重建的缓存，规则豁免它），再是违规者（`site_publisher.py`，渲染之前先删掉线上目录）。`run_check.py --round 1` 给第 1 轮已记录的 review 打分；不带参数则给三轮全部打分并做对比。两个命令即使判定为 FAIL，退出码也是 0。

用例是按它测试的规则命名的：`rel-1` 测 REL.1，`rel-2` 测 REL.2，依此类推。clone 自己的 `CODING-STYLE.md` 里有 REL.1 到 REL.4，而 `hr-demo` 没有（它只在第 3 节得到了 REL.1）。名字末尾的 `b` 表示同一条规则的第二个用例：`rel-3b` 换掉了 `rel-3` 的对照（见下面的“换成你自己的”），`rel-1b` 和 `rel-2b` 是留出的用例。

**你会看到。** oracle 以九行 `ok` 结尾，例如：

```text
ok   site_publisher: injected render failure reaches the caller: True (expected True)
ok   site_publisher: earlier pages still served after the failed publish: False (expected False)
ok   preview_cache: every preview is obtainable with current content after the failed rebuild: True (expected True)
```

`False (expected False)` 就是违规者丢了它的页面，这正是预期。第 1 轮会打印一张表，下面是其中四行（表尾还有两列 `cited`，是没有通过线的次要计数，这里省去）：

```text
case    clause  control    condition     runs ok  violation labeled  violation flagged  control labeled  control flagged  verdict
rel-1   REL.1   exempt     with_rule     3/3      3/3                3/3                3/3              3/3              FAIL
rel-1   REL.1   exempt     without_rule  3/3      0/3                3/3                0/3              3/3              -
rel-3b  REL.3   compliant  with_rule     3/3      2/3                3/3                0/3              3/3              PASS
rel-3b  REL.3   compliant  without_rule  3/3      0/3                3/3                0/3              3/3              -
...
PASS 1  FAIL 6  INCOMPLETE 0
```

运行以两行结束：`wrote samples/generalization/results-round-1.json and samples/generalization/results-round-1.md` 和 `samples/generalization/results-compare.md not written: --round scores only one round`（只有完整回放才会写对比文件）。完整回放会为三轮各打印一张表，第 2、3 轮下面还有 `notes:`，说那几轮之后规则变过；那两轮是用改写过的措辞录的，后来被回滚了（第 6 节）。在没改动的树上，`git status --short` 什么也不打印：回放逐字节重现了已提交的结果文件。

怎么读一行。每个用例每个条件有 3 次运行。`with_rule` 的 review 看到的是仓库的 digest；`without_rule` 的 review 看到的是去掉这一条条款之后的同一份 digest。*labeled* 表示该文件上某条发现把这条条款的 ID 当作 category；*flagged* 表示该文件上有发现，标签不限。通过线只看 `with_rule`：违规文件在至少 3 次里有 2 次被打上标签，并且对照文件 3 次里一次都没被打上。

把两个条件放在一起看。没有规则时，违规文件 3 次里 3 次被报出、0 次被打标签：reviewer 反正找得到这个 bug，规则改变的是标签。有规则时，`rel-1` 给被豁免的对照打标签的次数和给违规文件打的一样多（3 次对 3 次），所以过不了通过线。第 1 轮只有 `rel-3b` 通过。要像 README 一样平实地说：这个检验很小（4 条规则、7 个用例、每个条件 3 次运行、一个 reviewer 版本、一位作者），说得最多的是“还没有”。

**换成你自己的。** 离线、不用 reviewer：选你自己的一条规则，写一个违规文件和一个相似的对照（合规，或者属于规则明文豁免的情形），再按 `samples/generalization/cases/rel-1/oracle.py` 的形状写一个 `oracle.py`：注入一个故障，每个事实打印一行，只有违规者坏掉、对照没坏时才退出 0。把这三个文件放进你自己的用例目录（`samples/generalization/cases/mine-1/`，两个代码文件放在 `code/` 下；目录结构见[泛化检验的 README](samples/generalization/README.md#layout)），然后运行 `python3 -I samples/generalization/cases/mine-1/oracle.py`。oracle 让对照保持诚实。在这个项目里，`rel-3` 的对照被发现是有缺陷的：reviewer 是对的，用例错了，所以才有了 `rel-3b`。用真实 reviewer 录你的用例是要花钱的一步；第 6 节末尾的“换成你自己的”写了步骤和花费。

**检查点。** 你能读懂一行并说出每个计数是什么，能说出通过线，也能解释为什么“没有规则时违规文件 3 次里 3 次被报出”既不说明这条规则没用，也不说明它管用。

**讨论题。** 一条规则要做到什么，才算“泛化”？为什么这个检验要在每个违规文件旁边放一个对照？

**延伸阅读。** [泛化检验：目录结构](samples/generalization/README.md#layout) · [打分与 `--live`](samples/generalization/README.md#score-and-record) · [预先登记的判据与附录](samples/generalization/CRITERIA.md)

---

## 5. 自动化，以及它花多少钱（约 10 分钟）

**目标。** 看清让这个循环自己跑起来的几个部件（hook、阈值、限额、插件），以及限制花费的那些设置。wrap-up 是对上次 review 以来的工作做的一次 review，只有改动量够大时才运行；这一节讲的就是它什么时候运行、什么时候被拦住。所有命令都在离线状态下运行；唯一一个可能花钱的可选步骤会特别标出。

**命令。** 在第 3 节的临时仓库里。先看：现在会不会跑一次 wrap-up review？测量只用本地 git，不计费。

```sh
cd "$HR_SRC/../hr-demo"
house-rules wrapup --dry-run
seq 1 25 | sed 's/.*/value_& = &/' >> config_store.py
house-rules wrapup --dry-run
cat > .house-rules.json <<'EOF'
{
  "rules_files": ["CODING-STYLE.md"],
  "wrapup_auto": false,
  "wrapup_min_changed_lines": 20
}
EOF
house-rules wrapup --dry-run
```

接着，用一个 commit trailer 关掉一条发现，再手工记录一个判断。`heldout_unsafe.py` 从没被提交过（第 1 节只提交了配置文件），所以这次提交把它作为新文件加进去；关掉发现的是那个 trailer：

```sh
cp heldout_safe.py heldout_unsafe.py
git add heldout_unsafe.py
git commit -m "fix: serialize before opening the file" -m "Fixes-Finding: live-r3-1"
house-rules count --by-rule --label-source
house-rules label live-r3-2 --outcome dismissed --note "real, but outside this sample"
house-rules count --by-rule --label-source
```

最后是轮数上限。上面那次提交移动了 `main`，而在默认分支上每个新提交都是一个新的改动，所以它的轮数从 0 重新开始（[Review rounds](docs/LEDGER.md#review-rounds)）。再导入三次，用新的 run ID，把它推到 3 比 3；第四次会在任何 reviewer 运行之前就被拒绝。这会往 ledger 里加重复的发现，在临时仓库里无所谓：

```sh
for r in again-1 again-2 again-3 again-4; do house-rules review --from-file "$HR_SRC/samples/config-replacement/recorded-review.txt" --run $r; done
```

**你会看到。** 第一次试运行把你刚做的规则改动量成了零（规则文件、digest 和 changelog 不算工作量），所以跳过：

```text
wrapup: 0 changed line(s) in 0 file(s) since <tree> (the working tree as review live-r3 found it)
wrapup: skipped, changed lines 0 < wrapup_min_changed_lines 200 and changed files 0 < wrapup_min_files 5; no review run
wrapup: open unmapped findings: 2
```

加了 25 行之后仍然跳过（`25 < 200`，`1 < 5`）。把行数阈值降到 20 之后，配置文件的改动也被算进去，报告说该跑一次 review 了；数字会和下面接近：

```text
wrapup: 32 changed line(s) in 2 file(s) since <tree> (the working tree as review live-r3 found it)
[dry-run] wrapup: changed lines 32 >= wrapup_min_changed_lines 20; would run `claude -p '/code-review high <range>'`; nothing run or written
```

两个阈值，达到任何一个就够。带 trailer 的提交会打印 `infer: +1 fixed (auto:commit), +0 fixed (auto:gate_pass); open 1`，`count` 现在显示 `1 open`，而不是第 2 节里的 `2 open`，它的最后一行是 `review rounds on main@<12 hex>: 0/3`（第 2 节里是 `1/3`；这次提交开始了一个新的改动）；`label` 打印 `label: 1 human outcome(s), dismissed (live-r3-2)`，`count` 随后显示 `0 open`。轮数上限循环的第一次导入会出现新东西：同一份记录里的文字现在被解析成了 `heldout_unsafe.py:7` 上的 `REL.1`，因为这条条款已经存在了。第 2、3 次退出码为 0；第 4 次被拒绝，退出码 3：

```text
house-rules: error: round cap reached for main@<12 hex>: 3/3 rounds used; 6 findings still open (house-rules count). Fix blockers, let the rest go to distill, or rerun with --over-cap (recorded).
```

**各项限额。** 这些是限定自动化能做什么、能花多少的旋钮。它们都在 `.house-rules.json` 里（[带默认值的表](docs/LEDGER.md#config-house-rulesjson)）：

| 旋钮 | 默认值 | 它限制什么 |
|---|---|---|
| `wrapup_min_changed_lines`、`wrapup_min_files` | 200、5 | 两者都没达到时，不跑 wrap-up review，也不计费。达到任何一个就够 |
| `max_review_rounds` | 3 | 每个改动的 review 轮数；下一次会在 reviewer 运行之前被拒绝（`--over-cap` 可以越过，并留下记录） |
| `wrapup_auto` | 除非 `init` 打开，否则关闭 | 一次提交能不能自己启动一次 wrap-up |
| `wrapup_auto_max_per_day` | 2 | 任意滚动 24 小时内被请求或已启动的自动 review 次数；超过后退出码 4，并只提示一次。记账文件是 `.house-rules/auto-reservations.jsonl` |
| `digest_max_bytes` | 4096 | digest 最多能长到多大；会把它推过上限的规则会被拒绝 |

**装上插件（需要 Claude Code；不在免费路径里）。** 安装插件，每个仓库运行一次 setup，之后照常提交。自上次 review 以来的工作量达到某个阈值时，Stop hook（Claude Code 在一轮对话结束时运行的 hook）会在后台启动一次 wrap-up review，最多问你一个问题，而且只在出现新的盲点时才问。这需要 Claude Code 2.1.163 或更新版本。

```bash
claude plugin marketplace add wangxuzhou666-arch/house-rules
claude plugin install house-rules@house-rules
```

然后在你的仓库里运行 `/house-rules:setup`。其余的技能是 `/house-rules:review`、`/house-rules:wrapup` 和 `/house-rules:maintain`。

**可选，要花钱。** 在你自己的某个仓库里跑一次真实的 review：`house-rules review --level high`（或 `/house-rules:wrapup`）。作者实际跑下来，一个两文件的改动大约花 0.2 美元、约 30 秒；一个特别大的范围要贵得多（有一个约 9000 行的范围用掉了数百万 token）。这是估计，不是记录下来的数字，真实的数字取决于你的套餐、模型和改动。

**换成你自己的。** 把你自己的数字写进 `.house-rules.json`，反复运行 `wrapup --dry-run`，直到你能预测：你自己仓库里最近这段工作，会不会触发一次 review。

**检查点。** 你能说出：什么会启动一次 review，什么会拦住它（阈值、轮数上限、每日上限），每日上限的记账文件在哪，以及一个 commit trailer 做了什么。

**延伸阅读。** [Review rounds](docs/LEDGER.md#review-rounds) · [Thresholds](docs/WRAPUP.md#thresholds) · [The daily cap](docs/WRAPUP.md#the-daily-cap) · [Cost, limits and versions](docs/WRAPUP.md#cost-limits-and-versions) · [If nothing happens](docs/WRAPUP.md#if-nothing-happens) · [Cost](docs/INTEGRATION.md#cost) · [With and without the plugin](docs/INTEGRATION.md#with-and-without-the-plugin)

---

## 6. 调你的标尺（约 10 分钟）

**目标。** 试着改进一条规则，并弄清楚为什么回放没法告诉你改进了没有。项目自己那次调优尝试，以及它哪里出了问题，会成为你的讨论材料。

**命令。** 在 clone 里，它的 `CODING-STYLE.md` 里有 REL.1。改写 REL.1 的 digest 行，重新编译，再回放一次第 1 轮，看看变了什么。这些改动会在最后一步撤销。

```sh
cd "$HR_SRC"
python3 - <<'EOF'
p = "CODING-STYLE.md"
t = open(p, encoding="utf-8").read()
old = "Keep usable state until replacement is ready and validated; retain or restore it on failure; disposable rebuildable temporary artifacts are exempt"
new = "Never delete the old state before the new one is ready; restore it on failure; rebuildable temporary files are exempt"
assert t.count(old) == 1
open(p, "w", encoding="utf-8").write(t.replace(old, new))
EOF
house-rules compile
python3 samples/generalization/run_check.py --round 1 | tail -10
git status --short
git diff samples/generalization/results-round-1.md
python3 samples/generalization/run_check.py --live --round 4 --case rel-1
```

**你会看到。** digest 变短了（`compile: 1 written, 0 unchanged; digest CODING-STYLE.digest.md: 4053 bytes (limit 4096)`）。回放打印的分数和之前一样，外加每个受影响的用例一条提示。最后十行（两行图例在这里缩短了）以总数开头，`PASS 1  FAIL 6  INCOMPLETE 0`，判定和第 4 节里的相同：

```text
PASS 1  FAIL 6  INCOMPLETE 0
labeled = a finding carries the case's clause as its category; ...
cited = secondary, declared after round 1, ...
run slots that needed a repeat after a failed first attempt (both attempts are kept): 0

notes:
  rel-1: the rules changed since recording: the REL.1 line of the digest now differs from the recorded one; the scores describe the recorded digests
  rel-1b: the rules changed since recording: ...
wrote samples/generalization/results-round-1.json and samples/generalization/results-round-1.md
samples/generalization/results-compare.md not written: --round scores only one round
```

`git status` 显示 `CODING-STYLE.md`、`CODING-STYLE.digest.md` 和 `samples/generalization/results-round-1.*` 被修改了。回放把提示写进了被跟踪的结果文件（`git diff` 在那里显示的就是这些：分数原封不动，下面多了一个 `Notes:` 列表），所以最后一步要把它撤销。live 命令只给出计划；它不发起任何调用，退出码为 2：

```text
not started: pass --yes-spend 6 to confirm exactly this many calls
live plan: 6 reviewer call(s) planned into round 4 (1 case(s) x 2 conditions x 3 run(s) at /code-review high), plus at most 6 more as repeats ...; this spends real money
```

要点：回放拿每份记录对照它当时所用的 digest 打分，所以改规则永远动不了分数。只有新的一轮 live 才能说明你的改动有没有帮助，而那是要花真 review 的钱的：一个用例 6 次调用，七个用例全做是 42 次。

**可选，要花钱。** 如果你有 Claude Code，想知道一个用例的答案，就用上面的计划命令加 `--yes-spend 6` 来跑，此时你的改动还在；然后回放，读新的一轮：

```bash
python3 samples/generalization/run_check.py --live --round 4 --case rel-1 --yes-spend 6
python3 samples/generalization/run_check.py
```

按每次 review 约 0.2 美元（作者的估计）算，大约是 1.2 美元，花几分钟。记录会留在你的 clone 里；不管结果如何，都把它写下来。

**撤销。** 恢复你的 clone（你录下的记录会作为未跟踪文件留下）：

```sh
git checkout -- CODING-STYLE.md CODING-STYLE.digest.md samples/generalization
```

**讨论：项目自己的调优尝试发现了什么。** 作者改写了 REL.1 和 REL.2，让它们的 digest 行以豁免条件开头，缩短了 Labeling 指令，又录了两轮（多出 72 次 review）。三轮全都在 `results-compare.md` 里。

1. **把豁免写进 digest，没能阻止误标。** `rel-1` 里被豁免的对照，在第 1 轮 3 次里有 3 次被标成 REL.1，第 3 轮换了新措辞后仍然是 3 次里 3 次（留出的 `rel-1b`：两次都是 3 次里 2 次）。不管有没有这条规则，reviewer 都会把对照里的问题报出来；规则改变的是标签。
2. **缩短 Labeling 指令，让 `category` 消失了。** 这是从计数里推断出来的，不是检验过的原因。数一数 `with_rule` 的运行中，输出里任何地方都没有 `category` 的有几次：

   ```sh
   cd "$HR_SRC/samples/generalization/recordings"
   for r in 1 2 3; do echo "round $r: $(grep -L '"category"' round-$r/*/with_rule-?.txt | wc -l | tr -d ' ') of $(ls round-$r/*/with_rule-?.txt | wc -l | tr -d ' ') runs have no category"; done
   ```

   你会得到 21 次里的 7 次，然后是 18 次里的 17 次，然后是 18 次里的 6 次：标签在第 2 轮丢了，旧文本回来之后它也回来了。薄弱的环节是标签，不是发现本身。
3. **每个条件 3 次运行，噪声很大。** `rel-4` 的条款文本在第 1 轮和第 3 轮之间没有变，它的违规文件却从 3 次里 1 次被标，变成了 3 次里 3 次。

结果：改写没有带来改善，所以作者把 REL.1 和 REL.2 回滚到了第 1 轮的措辞；今天的 digest 与第 1 轮的逐字节相同。问问大家：你接下来会测什么？怎样才能让一个 3 次运行的结果可信：更多次数、固定模型、第二个 reviewer、换一种标签？

**换成你自己的。** 拿你在第 1 或第 3 节里的一条自己的规则，改写它的 digest 行，然后在跑任何收费的运行之前，先写下你预测 reviewer 会给什么标签、会怎么处理你的对照。如果你有预算，就用 `--live` 录你自己的一个用例，并如实记下结果，不管通过还是失败。准备工作是免费的、离线的；只有最后一步要花钱：

1. 这个检验读的是 clone 里的规则，不是 `hr-demo` 的。把你的条款加进 clone 的 `CODING-STYLE.md`，然后运行 `house-rules compile`。clone 的 digest 只剩大约 14 字节的余量，所以任何新条款都会把它推过 4096 字节，`compile` 会发出警告（一条单行条款的情形是 `warning: digest CODING-STYLE.digest.md is 4133 bytes, over digest_max_bytes 4096`；你的数字会不一样）；它仍然会写出 digest。新条款同时也给新的一轮提供了它需要的 digest，因为每一轮必须用一份以前没有哪一轮用过的 digest。
2. 拿一个现成用例当模板：`cp -r samples/generalization/cases/rel-1 samples/generalization/cases/mine-1`，把 `code/` 里的两个文件换成你的违规文件和你的对照（两个不同的文件名），再像第 4 节那样重写 `oracle.py`。
3. 编辑 `samples/generalization/cases/mine-1/case.json`。检验脚本会读 `v`（保持 1）、`id`（目录名）、`clause`（你的条款 ID）、`violation` 和 `control`（`code/` 里的路径）以及 `control_kind`（`exempt` 或 `compliant`）；其余字段只是给人看的用例说明。
4. `python3 -I samples/generalization/cases/mine-1/oracle.py` 必须退出 0；`python3 samples/generalization/run_check.py --live --round 4 --case mine-1` 会打印计划并退出 2，不发起任何调用，和上面的命令一样。
5. 要花钱的一步：同一条命令加上 `--yes-spend 6`。这是 6 次 review，按作者的估计每次约 0.2 美元，大约 1.2 美元；失败的运行最多再重试 6 次：合计最多约 2.4 美元。所有选项都写在 [Score and record](samples/generalization/README.md#score-and-record) 里 `--live` 那一条。

要在 clone 里撤销这一切，再运行一遍上面的撤销命令，再 `rm -rf samples/generalization/cases/mine-1`（并删掉你录下的记录）。

**检查点。** 你能解释：为什么回放确认不了一次规则改动，弄清楚要花什么代价，以及用自己的话说出这三个教训。

**延伸阅读。** [判据与附录 2、3](samples/generalization/CRITERIA.md) · [这个循环为什么这样设计，以及它的局限（中文）](docs/METHOD.zh.md) · [Contributing：发一个合成的失败示例、一对回归用例、一个泛化用例或一条规则提案，永远不要发真实的私有发现](CONTRIBUTING.md)

---

## 你现在有了什么

一份带稳定条款 ID 的规则文件；一份 reviewer 和编码会话都会读的 digest；一个记录发现及其处置结果的 ledger；一份 changelog，记着每次规则改动、它的证据和那条决定；还有一套能问“这条规则管不管用”的办法。你没有的是 agent 因此写出更好代码的证据，这个项目也不主张有。

**在你自己的仓库里用起来。**

1. 装好它。工作坊里的 `export` 只在一个终端里有效。把 clone 的 `bin/` 放进 `PATH`，或者装 Claude Code 插件（见 README 的 [安装](README.zh.md#安装)）。
2. 在你的仓库里运行 `house-rules init --rules-file <your rules file>`（装了插件就用 `/house-rules:setup`），先用 `--dry-run` 预览。把 `init` 列出的文件一起提交：pre-commit hook 检查的是暂存区里的那一份。
3. 先拿到一次 review：`house-rules review --level high`（要花钱；见第 5 节），或者用 `house-rules review --from-file out.json --run ID` 导入另一个 reviewer 的 JSON。然后 `house-rules count --by-rule` 按条款显示还有什么没处理。
4. 打开任何自动化之前，先在 `.house-rules.json` 里设好阈值，反复运行 `house-rules wrapup --dry-run`，直到你能预测它会做什么（第 5 节）。
5. 只通过“决定”这一步来改规则：`/house-rules:maintain`（需要 Claude Code），或者 `decision-context` 加 `apply-decisions`（第 3 节），这样每次改动的证据和那条决定都留在 changelog 里。
6. 把“某条规则有帮助”的任何说法都当作未经证实的。回放确认不了它；只有一轮要花钱的 live 才行（第 4、6 节）。

清理：`rm -rf "$HR_SRC/../hr-demo" "$HR_SRC/../hr-mine"`（第二个只有在你做了第 1 节的“换成你自己的”时才存在）。除非你在第 6 节录过 live 用例，或者加过你自己的用例，否则 clone 没有变化。

接下来：[README（中文）](README.zh.md) 讲怎么装插件，[Fitting house-rules into daily work](docs/INTEGRATION.md) 讲日常使用，[这个循环为什么这样设计（中文）](docs/METHOD.zh.md) 讲设计和局限。
