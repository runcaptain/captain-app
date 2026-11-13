// CAPTAIN: the data the Weights page needs, in one place. The page reads and writes only these shapes, and only
// through adapter.js. Until Captain's services answer for weights, local.js stands in for them with the same
// shapes over the two starter networks. When the services land, adapter.js is the one file that changes.
//
// The routes adapter.js asks for (all JSON). They are the ones Captain's services serve under /api/weights
// (captain_search/weights/routes.py and docs/weights.md there):
//
//   GET  /api/weights/models                        -> [NetworkSummary]
//   GET  /api/weights/models/{model}?session=&watch=1 -> Network   (without a session it holds no numbers)
//   GET  /api/weights/models/{model}/session        -> Session   (the open session for this model, made if none)
//   GET  /api/weights/sessions/{session}            -> Session
//   POST /api/weights/sessions/{session}/edits      <- EditRequest   -> WeightEvent
//   POST /api/weights/sessions/{session}/undo       <- {edit: id, by} -> WeightEvent
//   POST /api/weights/sessions/{session}/save       <- {note, by} -> {model, version, ref, changed}
//   POST /api/weights/sessions/{session}/program    <- {goal, max_edits, target_input, target_output} -> {job, session, agent, goal}
//   POST /api/weights/sessions/{session}/program/stop -> {session, stopping}
//   GET  /api/jobs/{job}                            -> {status, message, result}: the result is the run's report
//
// A trained model too big to draw neuron by neuron (the services' kind "session-ranker": a small transformer with
// tree rankers) is a "model of parts". It is read here, never edited or run. adapter.js turns the services'
// answers into the shapes below:
//
//   GET  /api/weights/models/{model}                                  -> the description, no numbers -> PartsModel
//   GET  /api/weights/models/{model}/session                          -> the session the screen looks through
//   GET  /api/weights/sessions/{s}/heatmap?tensor=&rows=a:b&watch=1   -> Heatmap
//   GET  /api/weights/sessions/{s}/attention?layer=&head=&matrix=&watch=1 -> one head's matrix -> Heatmap
//   GET  /api/weights/sessions/{s}/nearest?tensor=&row=&top=&watch=1  -> NearRows
//   GET  /api/weights/models/{model}/trees?ranker=&trees=             -> Ranker
//
// `watch=1` is a screen's look: it is not counted against what an agent was allowed.
//
// Live topics (the app's change feed, kit/live.js): `weights:<model>` when the model's numbers change, and
// `weights-session:<session>` when the session's record gains an event. The page reads the session again on
// either, and the network again when an event says numbers changed.

/**
 * @typedef {object} NetworkSummary
 * @property {string} id            "starter:tiny", or a model's id in Captain
 * @property {string} name
 * @property {string} [says]        one sentence for the list
 * @property {number[]} [sizes]
 */

/**
 * A model of parts, as adapter.js `partsFrom` makes it from the services' description.
 * @typedef {object} PartsModel
 * @property {string} id
 * @property {string} name
 * @property {"parts"} kind
 * @property {string} kind_words      what it is, in words: "Session model with rankers"
 * @property {number} parameters      how many numbers it has
 * @property {string} [source]        where it came from, in one sentence
 * @property {string} [wiring]        what the services say about how sure the order of the parts is
 * @property {Part[]} parts           left to right, as the data flows
 * @property {{id: string, name: string, trees: number|null}[]} rankers   each is read whole (Ranker) when opened
 *
 * @typedef {object} Part
 * @property {string} id
 * @property {"embedding"|"projection"|"attention"|"feed_forward"|"norm"|"head"|"scale"|"other"} kind
 * @property {string} name
 * @property {string} says            the services' own sentence about it
 * @property {[number, number]} shape rows and columns of its main matrix
 * @property {number} parameters
 * @property {string[]} feeds_from    the parts whose output it reads
 * @property {number} [block]         which layer it belongs to, from 1
 * @property {number|null} heads      for attention. Null when the files do not say
 * @property {"session"|"candidate"|null} side
 * @property {string|null} embeds     for a table: what a row stands for, when known. `guessed` when from its name
 * @property {PartTensor[]} tensors
 *
 * @typedef {object} PartTensor       one matrix of a part that can be drawn
 * @property {string} id
 * @property {string} name            in plain words
 * @property {string} [technical]     its name in the training code
 * @property {[number, number]} [shape]
 * @property {boolean} [per_head]     one matrix per attention head
 * @property {object} how             how adapter.js asks for it
 *
 * @typedef {object} Ranker
 * @property {string} id
 * @property {string} name
 * @property {number|null} trees      how many trees it has
 * @property {{name: string, splits: number, gain_share: number}[]} inputs   gain_share from 0 to 1
 * @property {string[]} unused        inputs no tree ever asks about
 * @property {{tree: number, nodes: TreeNode[]}[]} shown   the first trees, whole
 *
 * @typedef {object} TreeNode         a split ({input, threshold, yes, no}: `yes` when the input is above the
 *                                    threshold) or a leaf ({leaf: what it adds to the score})
 * @property {number} id
 * @property {string} [input]
 * @property {number} [threshold]
 * @property {boolean} [categorical]
 * @property {number} [yes]
 * @property {number} [no]
 * @property {number} [leaf]
 *
 * @typedef {object} Heatmap
 * @property {string} tensor
 * @property {[number, number]} shape   the matrix's own size
 * @property {number} from              the first of its rows in `values` (a tall table is sent a window at a time)
 * @property {number[][]} values
 *
 * @typedef {object} NearRows           the rows of a table most like one row
 * @property {number} row               the row asked about
 * @property {string|null} [name]       what that row stands for
 * @property {{row: number, name?: string|null, similarity: number}[]} nearest   cosine similarity, from -1 to 1
 */

/**
 * A small fully connected network, whole. Only networks small enough to send whole open on this page.
 * @typedef {object} Network
 * @property {string} id
 * @property {string} name
 * @property {number[]} sizes       neurons per column, input first: [784, 16, 16, 10]
 * @property {"sigmoid"|"relu"} squish   what turns a weighted sum into an activation
 * @property {{W: number[][], b: number[]}[]} layers   layers[k].W[to][from] joins column k to column k + 1
 * @property {[number, number]} [inputShape]   rows and columns when the input is a picture: [28, 28]
 * @property {string[]} [inputNames]
 * @property {string[]} [outputNames]
 * @property {Example[]} examples   the inputs a person can choose
 * @property {Example[]} test       the test set the cost is averaged over
 * @property {number} [version]     the model's version
 * @property {boolean} [synthetic]  its data was made up by code
 */

/**
 * @typedef {object} Example
 * @property {string} name
 * @property {number[]} x           one number per input neuron
 * @property {number[]} y           the wanted output, one number per output neuron
 */

/**
 * What the agent was allowed and what it used. Written by the services, never by the agent or this page.
 * @typedef {object} Ledger
 * @property {boolean} raw_weights        the agent may read raw numbers into its context
 * @property {number} weights_read        how many it has read
 * @property {boolean} sandbox            the agent may run code on the numbers
 * @property {number} forward_allowed     how many times it may run the network
 * @property {number} forward_used
 * @property {boolean} gradients_computed
 * @property {boolean} [feedback]         false for a blind session: its scores are hidden until it is saved or closed
 * @property {boolean} [scores_shown]     a score was shown to the caller
 * @property {boolean} [weights_alone]    worked out by the services from the whole record: no sandbox, no forward
 *                                        pass, and no score shown before the last edit
 * @property {number} [screen_looks]      how many times a screen read the numbers (never counted against the agent)
 * @property {number} changed             how many weights and biases differ from the base
 * @property {number} total               how many the network has
 */

/**
 * @typedef {object} Session
 * @property {string} id
 * @property {string} model
 * @property {Ledger} ledger
 * @property {WeightEvent[]} events       oldest first
 * @property {{held_out?: {defined: boolean, of?: number, base?: {model: string, cost: number, right: number, of: number}}}} [sets]
 *           held_out.base: how the version the session opened on does on its own test set, before any edit
 */

/**
 * One thing done to the network: a read, a run, an edit or an undo, by the agent or by the person.
 * @typedef {object} WeightEvent
 * @property {string} id
 * @property {number} seq
 * @property {string} at                  ISO time
 * @property {"agent"|"you"} by
 * @property {"read"|"run"|"edit"|"undo"} kind
 * @property {{neurons: [number, number][], weights: [number, number, number][]}} touched
 *           neurons as [column, index]; weights as [layer, to, from]
 * @property {"set"|"scale"|"zero"} [op]
 * @property {number} [layer]             the column of the neuron edited (1 is the first after the input)
 * @property {number} [neuron]
 * @property {number} [factor]            for scale
 * @property {string} [why]               the one sentence given before the edit
 * @property {Change[]} [changes]         every number that changed, old and new
 * @property {EditCost} [cost]
 * @property {boolean} [undone]           an edit that was taken back stays in the record, marked
 * @property {string} [undoes]            for an undo: the edit's id
 * @property {string} [input]             for a run: the input's name
 */

/**
 * @typedef {object} Change
 * @property {number} k                   the layer
 * @property {number} j                   the neuron the number belongs to
 * @property {number|null} i              the neuron the weight comes from, or null for the bias
 * @property {number} old
 * @property {number} new
 */

/**
 * The three numbers of an edit, each before and after.
 * @typedef {object} EditCost
 * @property {{name: string, before: number, after: number}} target   the input the edit was meant to fix
 * @property {{before: number, after: number, of: number, answers_changed: number}} keep   the other inputs: should not change
 * @property {{before: number, after: number, of: number, right_before: number, right_after: number}} test
 */

/**
 * @typedef {object} EditRequest
 * @property {"set"|"scale"|"zero"} op
 * @property {number} layer               the neuron's column
 * @property {number} neuron
 * @property {number|null} [from]         for set: which incoming weight, or null for the bias
 * @property {number} [value]             for set
 * @property {number} [factor]            for scale
 * @property {number} [target]            which of the network's examples the edit is meant to fix: the services then
 *                                        measure "should not change" on the other examples
 * @property {string} [why]
 * @property {"agent"|"you"} by
 */
export {};
