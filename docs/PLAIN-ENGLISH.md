# What settle does, in plain English

Written for the person who has to reconcile the bank statement, not the person
who has to maintain the code. No technical background assumed.

## The job it does

Money arrives in your bank account and somebody has to work out what it was for.
Which invoices did this payment settle? On a good day that's quick and dull.
Most days it isn't.

A single transfer turns out to cover three invoices. A customer pays two-thirds
now and promises the rest next month. An international payment lands 2.9% light
because a correspondent bank helped itself to a fee on the way through. The
reference field says `INV-2026-0042`, or `szamla 42`, or `0042/2026`, or
something that made sense to the customer and to nobody since. And every so
often two invoices for the same customer are for the same amount on the same
day, and there is honestly no way to tell which one the money was for.

Finance teams get through this by hand, line by line, every month. settle does
the first pass for you.

## What goes in, what comes out

You give it two files: your open invoices, and your bank statement. Both are
ordinary spreadsheet exports, the sort most accounting systems will produce
without anyone doing anything clever.

Back comes a list of which payment paid which invoice, the closing position of
every invoice (paid, part paid, still open, or closed after a bank fee), a
shortlist of the payments it couldn't resolve, and a summary of the run.

Every decision has its reason written next to it, in words. "Exact amount and
reference." "Combination of 3 invoices, reference matched all 3." "Amount within
fee tolerance, no reference." You never have to take its word for anything. Open
the file, read why it did what it did, and overrule it where you disagree.

## It doesn't guess

If two invoices explain a payment equally well, settle won't pick one. It shows
you both and puts the payment on the review list.

That sounds like a minor detail. It's the whole point. Something that's right
95% of the time and quietly wrong the rest is no use in accounting, because you
can't tell which is which, so you end up checking everything anyway and you've
saved nothing. The design is arranged so that an answer from settle is one you
can act on, and anything it isn't certain about comes to you instead.

The arithmetic works the same way. It counts in exact amounts rather than
approximations, so nothing vanishes into rounding, and after each run it checks
its own totals: every cent that came in has to be accounted for somewhere. If
that check fails it refuses to hand over the result at all, rather than give you
one that doesn't add up.

## How well it works

This part was measured, not asserted. It was tested against realistic invented
data where the right answers were already known, and then the data was made
steadily messier.

The pattern holds all the way down. As the mess increases settle doesn't start
getting things wrong. It stays right when it answers, between 97 and 100% of the
time across the whole range. What changes is how often it asks for help. Given
clean data it handles the lot on its own. On the messiest data tested it
resolved roughly seven payments in ten and passed the rest back.

Mess costs you time, in other words, not accuracy. For bookkeeping that's the
right way round.

## Something worth knowing even if you never use it

No single kind of mess defeats it, because there's usually a second clue to fall
back on. Ruin the references and the amounts still pick most payments out on
their own.

The trouble starts when reference problems turn up alongside something else.
Once the reference is unreadable the amount is all that's left, and fees, part
payments and merged transfers are precisely the things that make the amount
unreliable. That combination is where the work starts coming back to you.

Which points at something you can act on without buying anything: getting
customers to put a usable reference on their payments will do more for you than
any clever matching software, this one included.

## Using it without a terminal

There is a browser version, [`settle-app`](../app/README.md). You sign in, pick
your two files, check that it has worked out which column is which, and get the
same answers on a screen instead of in a folder. It keeps a history, so last
month's reconciliation is still there when someone asks about it.

Everything on those screens is a view of the same files described above, and
every table has a download link next to it. Nothing is calculated differently
for the web — it is the same engine, and there is a test whose only job is to
prove the file it hands you is identical to the one the command line would have
written.

Your files also don't have to use settle's column names. It reads the headers,
works out that your `Invoice Number` is its `number`, and shows you what it
concluded before anything runs. If it guesses wrong, you change it on that
screen.

## What it isn't

settle is an engine, not a finished product. It doesn't plug into your bank and
it doesn't read your accounting system directly. Two files in, answers out. No
credentials to hand anyone, and no data leaving wherever you choose to run it.

Connecting it to your own systems is a separate piece of work. It was left out
deliberately, not forgotten.

One thing worth knowing if you run the browser version: unlike the command-line
one, it does keep what you upload, so that the history works. Who can read it
and for how long are both your decisions, and
[the deployment guide](DEPLOY.md) spells out what those decisions are.

## What it costs

Free for individuals, students, researchers, teachers and nonprofits. Businesses
need a paid licence — nagytmas@gmail.com, and the terms are negotiable.

Reading the code and trying it out is free for everyone either way, a company
deciding whether it's worth paying for included. The full terms are in
[`COMMERCIAL.md`](../COMMERCIAL.md).
