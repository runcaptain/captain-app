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
//
// A trained model too big to draw neuron by neuron (a transformer with tree rankers, say) is a "model of parts".
// It is read here, never edited or run:
//
//   GET  /api/weights/models/{model}                              -> PartsModel (kind "parts")
//   GET  /api/weights/models/{model}/parts/{part}/heatmap?tensor=&head=&max= -> Heatmap
//   GET  /api/weights/models/{model}/parts/{part}/rows?tensor=&head=&top=    -> [RowStats]
//   GET  /api/weights/models/{model}/parts/{part}/nearest?row=&top=          -> NearRows
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
 * A model of parts. A NetworkSummary for one carries kind, kind_words, parameters and source too.
 * @typedef {object} PartsModel
 * @property {string} id
 * @property {string} name
 * @property {"parts"} kind
 * @property {string} kind_words      what it is, in words: "Session model with rankers"
 * @property {number} parameters      how many numbers it has
 * @property {string} [source]        where it came from, in one sentence
 * @property {number} [width]         the model's own width
 * @property {Part[]} parts           left to right, as the data flows
 * @property {Ranker[]} [rankers]
 *
 * @typedef {object} Part
 * @property {string} id
 * @property {"embedding"|"projection"|"attention"|"feed_forward"|"norm"|"head"} kind
 * @property {string} name
 * @property {[number, number]} shape rows and columns of its main matrix
 * @property {number} parameters
 * @property {string[]} [feeds_from]  the parts whose output it reads
 * @property {number} [block]         which attention block it belongs to, from 1
 * @property {number} [heads]         for attention
 * @property {string} [rows_are]      for an embedding table: what a row stands for ("items")
 * @property {{id: string, name: string, shape: [number, number], parameters: number, per_head?: boolean}[]} tensors
 *
 * @typedef {object} Ranker
 * @property {string} id
 * @property {string} name
 * @property {number} trees           how many trees it has
 * @property {{name: string, splits: number, gain_share: number}[]} inputs   gain_share from 0 to 1
 * @property {string[]} unused        inputs no tree ever splits on
 * @property {{tree: number, nodes: TreeNode[]}[]} shown   the first trees, whole
 *
 * @typedef {object} TreeNode         a split ({input, threshold, yes, no}: `yes` when the input is above the
 *                                    threshold) or a leaf ({leaf: what it adds to the score})
 * @property {number} id
 * @property {string} [input]
 * @property {number} [threshold]
 * @property {number} [yes]
 * @property {number} [no]
 * @property {number} [leaf]
 *
 * @typedef {object} Heatmap
 * @property {string} tensor
 * @property {[number, number]} shape   the matrix's own size
 * @property {[number, number]} shown   the size of `values`
 * @property {boolean} reduced          the services averaged it down to fit
 * @property {number[][]} values
 *
 * @typedef {object} RowStats
 * @property {number} row
 * @property {string|null} [name]
 * @property {number} norm              the row's length as a vector
 * @property {number} mean
 * @property {number} largest           its largest number, ignoring sign
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
