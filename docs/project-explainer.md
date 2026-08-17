# What this project was, and what it found

This is a question-answering system over two pieces of European law: the EU AI
Act and the GDPR. You ask it a question in plain English, it finds the relevant
passages of the law, and it writes an answer with citations back to the exact
paragraphs it used. The bet was that storing the law twice — once as searchable
text, once as a structured map of how its provisions relate to each other —
would beat storing it once. It did not. The structured map made answers slightly
worse, cost about 85% more, and the one change that did help worked by taking
material *out* of the system rather than putting more in.

A note on two terms used throughout. **RAG** ("retrieval-augmented generation")
means: search a document collection for relevant passages, then hand those
passages to a language model and ask it to answer using only those. A
**knowledge graph** means: instead of storing text, store facts as connections —
"Article 26 imposes an obligation on deployers", "Article 99 references Article
16" — so you can follow links between them.

---

## 1. Why the hybrid — graph plus text search — is not the better approach

The system was built with two ways of finding law. The first is **vector
search**: every paragraph of the law is turned into a list of numbers (an
**embedding**) that represents its meaning, and a question is turned into the
same kind of list, so the system can find paragraphs whose meaning is close to
the question's. The second is the **knowledge graph**: an AI model read all 1,108
paragraphs and pulled out the facts and relationships it found, which were then
stored as a network of connections.

To measure them, 100 questions were written by hand, and for each one the exact
paragraphs of law that a correct answer needs were listed in advance. Those
paragraphs are called the **gold** — the right answer's evidence, agreed before
any measurement. Across all 100 questions there are 203 gold paragraphs.
**Recall** is simply the share of them a method manages to find.

Three findings, in the order they matter.

**The graph is a lossy copy of the documents.** Vector search reaches 77.3% of
the gold paragraphs. The graph reaches 36.0%. The graph is not a richer view of
the law; it is a smaller one. That makes sense in hindsight: an extraction model
reading a paragraph and writing down the facts it noticed will always lose
something, and what it loses is invisible until you go looking for it.

**The real bottleneck was choosing, not finding.** The system finds 77.3% of the
needed paragraphs, but it only puts five of them into the prompt. After that cut,
49.3% of the gold survives. So more than a quarter of what was correctly found is
thrown away by the step that picks the top five. Breaking the losses apart: of
203 gold paragraphs, 46 are never found at all, 6 are found and ranked highly but
squeezed out by the five-slot limit, and **51 are found, would have fit, and are
simply ranked below irrelevant text.** The single biggest loss is bad ordering.
Adding a second source of documents does nothing about that — it makes the
ranking job harder by giving it more to sort.

**The questions people actually ask do not name articles.** The graph's most
distinctive trick is following citations: the law says "without prejudice to
Article 16", and the graph can walk that link backwards to find which penalty
applies to an Article 16 breach. That works. It just almost never comes up. Of
the 90 usable questions, only 15 name an article or annex at all. The rest are
topical — "an employer uses an AI system to screen job candidates, what applies?"
— and there is no article number in them to start from. Across all 90 questions,
citation-following recovered **4** paragraphs that ranking had missed.

**The graph didn't fail because it was wired up badly; it failed because it found
less than plain search, and because the system's real problem was ranking, which
a second source makes worse rather than better.**

---

## 2. When a graph *would* be the right choice

None of the above says knowledge graphs are a bad idea. It says they were the
wrong idea for this corpus and these questions. Three conditions would have
changed the verdict.

**When the relationships are the content, not a description of it.** In this
project, the graph was a summary of text that already existed — every fact in it
was derived from a paragraph, so the paragraph was always available and always
more complete. That is the losing shape. The winning shape is where the
relationships exist nowhere as prose: an org chart, a supply chain, a software
dependency tree, a corporate ownership structure. "Which teams depend on a
service that depends on this library?" has no paragraph to retrieve. Text search
cannot answer it at any quality, because the answer is a path, not a passage.

**When users ask referential questions rather than topical ones.** The graph's
one genuine win here was following a citation from a named article. If the users
were compliance officers who habitually ask "what cross-references Article 6(2)?"
rather than "what applies to hiring software?", the 15-out-of-90 figure would
invert, and so would the value of traversal.

**When the documents are not made of near-identical paragraphs.** Legal text is
unusually hostile: Article 99 and Article 100 both set out fine tiers in nearly
identical language for different bodies, and Annex III lists eight parallel
high-risk categories that read the same way. Both the graph extraction and the
ranking stage struggle to tell those apart. A corpus of distinct, self-contained
documents would not punish either component this hard.

**For the graph to have won here, the answers would have had to live in
connections rather than in paragraphs — and in this corpus, they live in
paragraphs.**

---

## 3. What could have been done differently

**Measure whether the bottleneck is finding or choosing, before adding a second
source.** The graph was built first and measured last. Had the loss breakdown —
46 lost in search, 6 to the cap, 51 to bad ordering — existed at the start, it
would have been obvious that the pipeline's weak point was the narrowing step,
and that no new retrieval source could help it. That measurement is cheap. It
costs no API calls and needs only the hand-written gold list. It should have been
the first thing built, not the fifth.

**Measure the noise floor before comparing anything.** The same system was run
twice on the same 100 questions, with the requests verified byte-for-byte
identical. The pass/fail result changed on 7.1% of questions purely from
run-to-run variation in the model. That puts a ±5.2 percentage point band of
uncertainty around every single-run score. The gap between the four systems being
compared was *smaller than that band*. Months of comparisons had been made
against differences that were not measurable. Any evaluation should establish
this number first, because it tells you the smallest difference your setup can
detect — and therefore which experiments are worth running at all.

**Notice the pattern in what works.** Every lever that added material to the
system failed: graph facts, a bigger passage limit, keyword search alongside
vector search. The two things that worked both *removed* material — reading a
provision in its own order instead of a ranked order, and then dropping the
ranked passages entirely. That is not a coincidence. When the constraint is a
model's ability to pick the right thing out of a pile, making the pile bigger is
the wrong move, however good the additions are on their own.

**Find out where the pipeline actually loses before choosing what to add, and be
suspicious of any fix whose mechanism is "give the system more".**

---

## 4. What was tested to support the conclusion

Ten interventions were built and measured. They are listed here in the order they
were tried, with the two most informative explained rather than just scored.

1. **Graph facts in the prompt.** On the 28 questions where the graph fired: 0
   wins, 4 losses against plain vector search. On one of them the system had
   already answered correctly without the graph, and adding 50 graph facts made
   it reply that it couldn't find the answer.
2. **The graph budget.** The graph produces hundreds of facts per question, so
   only the first 50 are sent. That cut keeps 20 of the 61 relevant items the
   graph found. Sending everything keeps more of them, but the model responds to a
   long list of near-identical facts by writing citation markup until it runs out
   of output. Neither setting works.
3. **Raising the passage limit from 5 to 8.** Three extra slots per question
   brought in 6 more gold paragraphs across the whole set — roughly 80% of the
   added material was noise.
4. **Adding keyword search alongside vector search.** Two classical keyword
   methods were unioned into the candidate pool. Gold paragraphs in the pool went
   from 157 to 176, the largest retrieval gain in the project. Gold paragraphs
   reaching the prompt went from 100 to 102. **This is the whole thesis of the
   project in one line: +19 found, +2 used.** The material moved from a stage
   that couldn't reach it to a stage that ranked it below noise. It is committed
   and switched off, because it doubles the ranking bill for two paragraphs.
5. **The reranker versus raw vector search.** A **reranker** is a second, slower
   model that re-scores search results by reading the question and each passage
   together. It was worth +2 paragraphs overall, which is inside the noise (p =
   0.711 — meaning a difference that size would show up by chance most of the
   time). It helps one category of question and hurts another.
6. **Using the graph to force variety** in the five chosen passages. Zero wins on
   any question category. The reason is instructive: on 21 of 90 questions the
   correct answer genuinely *is* several paragraphs of the same article, so a rule
   that punishes repetition throws away gold and noise at the same rate.
7. **Using the graph as a "well-connected" bonus** when ranking. Two-thirds of
   all candidates are already connected to something in the question, so the
   signal barely distinguishes anything. Best result: +2 paragraphs, not
   measurable.
8. **Following citations backwards.** The mechanism works and was worth 4
   recovered paragraphs across 90 questions, because only 15 questions name an
   article to start from.
9. **Enumeration — the one that worked.** Some questions ask for *all* of
   something: "list the obligations Article 26 places on deployers". The correct
   answer is eleven paragraphs, and asking a ranker for the best five is the wrong
   question. So for those questions the ranking stage is skipped entirely and the
   article is read straight out of the database in statutory order. Gold reaching
   the prompt on that category went from 12 of 48 to 33 of 48; across the whole
   set, 102 to 123 of 203. That is by far the largest retrieval gain measured.
10. **Answering enumerations paragraph by paragraph and stitching the results.**
    It worked, and it was unnecessary — see the next section.

The shipped result is the enumeration path in its stricter form: for these
questions, send *only* the enumerated provision and drop the ranked passages
altogether. It scores 4 of 10 on that category against the incumbent's 2 of 10,
and it is both cheaper ($0.0066 vs $0.0075 per question) and faster (5.8 s vs
8.4 s). It is the only change in the project that improved accuracy and cost at
the same time.

**Ten interventions, eight negative, and the one clear win came from deleting a
stage rather than adding one.**

---

## 5. What was learned

**One property of the corpus broke three separate things.** European legislation
is full of near-identical enumerated paragraphs: twelve limbs of Article 26,
eight parallel categories in Annex III, two articles setting out fine schedules in
matching language for different bodies. That single property independently broke
searching (the extraction model flattened parallel provisions together), choosing
(the ranker cannot tell Article 6(2) from Article 6(3), and sibling paragraphs
displace each other), and writing (the model reads a neighbouring article's
numbers as if they belonged to the one it was asked about). Three components, three
teams' worth of debugging, one root cause. Looking for the shared cause behind
unrelated-seeming failures turned out to be worth more than fixing any of them.

**Measurement was where most of the real findings came from.** Three in
particular.

The **noise floor**: running the identical system twice flips 7.1% of pass/fail
results, giving ±5.2 percentage points of uncertainty. Every comparison in the
project has to clear that bar, and most did not.

The **judge**: answers are graded by a language model, which agrees with
hand-grading 85% of the time overall and 95% on the pass/fail decision. That
sounded fine until a control arm caught it grading in favour of one of the
systems being tested. Three answers omitted a fact the grading rule explicitly
calls a partial credit, and were marked correct; the identical omission in
another system's answer was marked partial every time. The likely cause is
format — the favoured answers were longer and more densely cited. The lesson is
that an automated grader's accuracy is measured on one *shape* of answer, and
does not transfer to a system that writes differently.

The **wrong diagnosis**: one question about AI Act fine tiers kept producing an
answer with the wrong money attached to the wrong offences, even with all four
correct paragraphs in front of it. That was written up as a reasoning failure, and
a fairly elaborate fix was built for it. The actual cause was that Article 100 —
the fine schedule for EU institutions, with entirely different figures — was
sitting at ranks 1 and 3 of the passages the system had also been given. The model
was not reasoning badly. It was reading the wrong article, because the system had
handed it one. A cheap control arm that simply removed those passages fixed the
row; the elaborate fix bought nothing.

**One awkward property of the corpus broke retrieval, ranking and generation at
once — and most of what was learned came from measuring carefully enough to
notice that, including three occasions where the measurement caught the project
being wrong about itself.**

---

## 6. "How do you know you didn't just build the graph badly?"

This is the right question to ask, and it is the one the project spent the most
effort trying to answer honestly. Four things were checked.

Routing was ruled out: the system chooses whether to use the graph per question,
and it was rerun with hand-verified perfect routing. That version scored *one
answer worse*, so sending more questions to the graph is not the missing piece.
The budget was ruled out by measuring what it discards and by testing the
alternative, which fails in a different way. The traversal gap was closed: the
citation-following the graph was missing was built and measured, and it fires four
times in ninety questions. And the graph was tested in the one configuration that
avoids all prompt-related objections — as a scoring signal that never enters the
prompt at all — where it was also worth nothing.

What remains genuinely unproven is the extraction itself. The graph was built by a
language model reading each paragraph, and a better extraction would reach more
than 36%. But it would have to reach past 77.3% before it changed the conclusion,
and it is a compressed copy of the same text, so that seems unlikely rather than
impossible. The honest statement is: the graph as built loses, four independent
explanations for why were tested and eliminated, and the remaining explanation
would require the graph to beat the documents it was made from.

**Four possible excuses for the graph were tested and removed; the one that
survives would require a summary of the text to beat the text.**

---

## 7. "You spent months on this and the accuracy barely moved. What was the return?"

Fair. Overall accuracy sits around 39–42% correct depending on the configuration,
and that number did not move much. Two things are worth saying about it.

First, that headline conceals where the answers actually are. Roughly 42–46% of
answers are graded *partially correct* — for every configuration, at almost
exactly the same rate. Only fully correct counts as a pass. The systems are not
failing to find the law; they are failing to state it completely: a rule without
its exception, a fine tier without its small-business inversion, a chain that
stops one hop short. That is a writing problem wearing a search problem's
clothes, and this project spent its effort on the search half.

Second, the returns were real but they were not accuracy points. One shippable
improvement: the enumeration path, which is more accurate, cheaper and faster
than what it replaced. One strong negative result: a well-instrumented
demonstration that a hybrid graph does not help on this corpus, with the
mechanism identified rather than guessed. And one measurement asset: a
noise floor, a loss breakdown, and a graded evaluation set that makes the *next*
set of experiments cost hours instead of months.

The uncomfortable part I would not defend: the loss breakdown that redirected the
whole project should have been produced in week one, and it would have saved most
of the graph work. The value of a negative result is real, and it is worth less
than the same conclusion reached earlier.

**The return was a shippable win, a well-evidenced negative result, and an
evaluation setup that makes the next experiment cheap — but the finding that
redirected everything cost far more to reach than it should have.**

---

## 8. "How did you know your evaluation was trustworthy?"

It was not trusted; it was tested, and it failed several of those tests.

The evaluation is 100 hand-written questions, each with the specific paragraphs a
correct answer needs listed in advance, spread across categories of difficulty —
simple lookups, multi-step questions, questions spanning both regulations,
"list everything" questions, and questions that should be refused because the
answer isn't in the corpus. Answers are graded by a language model against a
per-question grading rule.

Four problems were found in the evaluation itself, all by deliberately probing it.
The grader was checked against 20 hand-graded answers and agreed 85% of the
time — then was later caught favouring one answer format, which means that 85%
does not transfer across systems that write differently. The output length limit
was silently deleting the hardest category's failures from the denominator,
because long answers were being cut off and cut-off answers were being dropped
from scoring. The published table used per-system denominators that differed
between systems; the corrected comparable figures are about a point lower for
everyone. And the run-to-run noise floor turned out to be wider than the entire
spread between the four systems under comparison.

What I would say plainly: the evaluation is good enough to support a clear
negative result, and it is too small to resolve the positive one. The enumeration
win rests on a category with only 10 questions, where a single question is worth
10 percentage points — twice the noise band. It is one win and zero losses, which
is encouraging, and it is not proof. Saying so is part of the result.

**Every check run on the evaluation found something wrong with it, which is why
its conclusions can be stated with confidence about what they cover — and its
limits stated just as plainly.**
