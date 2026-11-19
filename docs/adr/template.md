---
# These are optional elements. Feel free to remove any of them.
status: "{proposed | rejected | accepted | deprecated | ... | superseded by ADR-NNNN}"
date: "{YYYY-MM-DD when the decision was last updated}"
decision-makers: "{list everyone involved in the decision}"
consulted: "{list everyone whose opinions are sought (typically subject-matter experts); and with whom there is a two-way communication}"
informed: "{list everyone who is kept up-to-date on progress; and with whom there is a one-way communication}"
---

# {short title, representing solved problem and found solution}

This template follows MADR 4.0.0. Copy it to `ADR-NNNN-short-title.md` (four digits, lowercase hyphenated slug), fill it in and delete this paragraph. The ADR checker in `make docs-check` requires the front matter keys `status`, `date` and `decision-makers` and the five headings marked required below. In a real ADR, `status` is a plain word such as `accepted` and `date` is a real `YYYY-MM-DD` date.

## Context and Problem Statement

{Describe the context and problem statement, for example in free form using two to three sentences or in the form of an illustrative story. You may want to articulate the problem in form of a question and add links to collaboration boards or issue management systems.} (required)

<!-- This is an optional element. Feel free to remove. -->
## Decision Drivers

* {decision driver 1, for example a force, facing concern, ...}
* {decision driver 2, for example a force, facing concern, ...}
* ... <!-- numbers of drivers can vary -->

## Considered Options

* {title of option 1}
* {title of option 2}
* {title of option 3}
* ... <!-- numbers of options can vary -->

(required)

## Decision Outcome

Chosen option: "{title of option 1}", because {justification, for example only option which meets the knock-out criterion decision driver | which resolves force {force} | ... | comes out best (see "Pros and Cons of the Options" below)}. (required)

<!-- This is an optional element. Feel free to remove. -->
### Consequences

* Good, because {positive consequence, for example improvement of one or more desired qualities, ...}
* Bad, because {negative consequence, for example compromising one or more desired qualities, ...}
* ... <!-- numbers of consequences can vary -->

(The checker accepts the Consequences heading at any level. Keep it, since this project requires it.)

<!-- This is an optional element. Feel free to remove. -->
### Confirmation

{Describe how the implementation of, or compliance with, the ADR is confirmed. For example by a design or code review, a test, an architecture fitness function or a command. Name the concrete test or command.} (required)

<!-- This is an optional element. Feel free to remove. -->
## Pros and Cons of the Options

### {title of option 1}

<!-- This is an optional element. Feel free to remove. -->
{example | description | pointer to more information | ...}

* Good, because {argument a}
* Good, because {argument b}
<!-- use "neutral" if the given argument weights neither for good nor for bad -->
* Neutral, because {argument c}
* Bad, because {argument d}
* ... <!-- numbers of pros and cons can vary -->

### {title of other option}

{example | description | pointer to more information | ...}

* Good, because {argument a}
* Good, because {argument b}
* Neutral, because {argument c}
* Bad, because {argument d}
* ...

<!-- This is an optional element. Feel free to remove. -->
## More Information

{You might want to provide additional evidence/confidence for the decision outcome here and/or document the team agreement on the decision and/or define when/how this decision the decision should be realized and if/when it should be re-visited. Links to other decisions and resources might appear here as well.}
