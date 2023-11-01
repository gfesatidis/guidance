try:
    import torch
except ImportError:
    pass
import numpy as np
from .._utils import ByteTrie
from ._model import Model
# from ..library._string import string
from .._parser import EarleyCommitParser
from .._grammar import Terminal
# import numba

class Local(Model):
    def __init__(self, tokens, bos_token_id, eos_token_id=None, echo=True):
        super().__init__(echo)
        
        assert isinstance(tokens[0], bytes), "The tokens need to be provided as bytes!"

        self.tokens = tokens
        self.bos_token_id = bos_token_id
        self.bos_token = self.tokens[self.bos_token_id]
        self.eos_token_id = eos_token_id if eos_token_id is not None else bos_token_id
        self.eos_token = self.tokens[self.eos_token_id]

        # build a prefix tree of the tokens
        self._token_trie = ByteTrie(tokens, np.arange(len(tokens)))
        self._token_trie.match = True
        self._token_trie.match_version = 0

    def _get_logits(self, token_ids):
        '''A fake method designed to be overriden by subclasses.'''

        # pretend to extend the KV cache and update the log probs
        return torch.randn(len(self.tokens))

    def _longest_token_match(self, bytes):
        '''Greedy token matching.'''
        # if string.startswith("\n"):
        #     pass
        trie_pos = self._token_trie
        for i,c in enumerate(bytes):
            if c in trie_pos.children:
                trie_pos = trie_pos.children[c]
            else:
                return bytes[:i], trie_pos.value # note that if there are redudant tokens we choose the one stored in the trie
        if len(trie_pos.children) == 0:
            return bytes[:i+1], trie_pos.value
        else:
            return None,None # more than one token can match these bytes

    def __call__(self, grammar, max_tokens=100, n=1, top_p=1, temperature=0.0, ensure_bos_token=True, log_probs=False):
        assert n == 1, "Still need to add support for n > 1!"
        
        # get our current context in bytes
        prompt = str(self)
        prompt = bytes(prompt, encoding="utf8")

        # add the beginning of sequence token if needed
        if ensure_bos_token and not prompt.startswith(self.bos_token):
            prompt = self.bos_token + prompt
        
        # create a parser with a grammar that includes both our context and the passed grammar
        parser = EarleyCommitParser(prompt + grammar)

        # loop until we have generated a complete pattern
        token_ids = []
        token_byte_positions = []
        hidden_count = len(prompt) # we don't emit the prompt
        generated_pos = 0 
        sampled_token_ind = None
        token_count = 0
        earliest_possible_hidden = 10000000000
        while True: # each iteration generates one more token (and some of the associated bytes)

            # enforce the token limit
            if token_count >= max_tokens:
                break

            # note where we are starting for this token
            start_pos = parser.pos

            # walk down the trie as far as possible before computing the logits
            bytes_to_force_used = []
            retry_token_gen = False
            trie = self._token_trie
            trie.match_version += 1 # this invalidates all the match caches from the previous token
            while True:
                next_byte_mask = parser.next_byte_mask()
                next_byte_mask_sum = next_byte_mask.sum()
                
                # see if we reached a dead end of the grammar
                if next_byte_mask_sum == 0:
                    break

                # if there is only one possible next byte we can keep forcing
                elif next_byte_mask_sum == 1:

                    # look for valid children
                    next_byte = None
                    for byte in trie.children:
                        
                        # mark this trie node with an up-to-date match flag (may save work later)
                        node = trie.children[byte]
                        node.match_version = self._token_trie.match_version
                        node.match = next_byte_mask[byte[0]]
                        
                        # see if we found a match
                        if node.match:
                            next_byte = byte
                            break

                    # if we can't extend then this token is forced
                    if next_byte is None:
                        break
                    
                    # otherwise since there is only one possible next byte we keep going
                    else:
                        commit_point = parser.consume_byte(next_byte, log_prob=0.0)
                        
                        # if we are at a hidden commit point then we need to hide the bytes that match that node
                        if commit_point is not None and commit_point.node.hidden:
                            # assert earliest_possible_hidden <= commit_point.start, "We failed to track a hidden node in advance!"
                            # assert not (hidden_parent_start < commit_point.start), "We don't support nested hidden commit points yet!"

                            # This takes the item and commits to it as part of the parse and then shrinks it to zero width
                            # in other words this hides the item
                            parser.commit_and_collapse_item(commit_point)

                            # kept_bytes = b''

                            
                            
                            # keep the bytes we still need to emit
                            if start_pos < commit_point.start:
                                parser.shadow_rewind(start_pos)
                                #bytes_to_force = parser.bytes[start_pos:commit_point.start]
                                # for i in range(start_pos, commit_point.start):
                                #     parser.bytes_to_force[i] = parser.bytes
                                # bytes_to_force = 

                                # yield kept_bytes, False, 0.0, {}, {}
                                # hidden_count += len(kept_bytes)
                                # generated_pos = parser.pos - len(kept_bytes)
                                # retry_token_gen = True
                            
                            else:
                                # pop off any tokens that overlap the hidden bytes
                                i = len(token_byte_positions) - 1
                                while i >= 0 and token_byte_positions[i] > commit_point.start:
                                    token_ids.pop()
                                    token_byte_positions.pop()
                                    token_count -= 1
                                    i -= 1
                                # re-add any bytes we cut too far on
                                # bytes_to_force = parser.bytes[token_byte_positions[-1]:commit_point.start]
                                parser.shadow_rewind(token_byte_positions[-1])
                            retry_token_gen = True # this restarts us at the top of the outer token gen loop
                            break
                            # generated_pos = parser.pos # send this parser.bytes[generated_pos:commit_point.start]
                            # start_pos = parser.pos

                            # trie = self._token_trie
                            # trie.match_version += 1
                            # pass # trim the token and output history...
                        
                        # # if we are at a possibly hidden point then we track that
                        # elif hidden_parent_start <= parser.pos:
                        #     earliest_possible_hidden = min(earliest_possible_hidden, hidden_parent_start)

                        # # if we are at a non-hidden commit point then earlier things are either already hidden or won't be hidden
                        # elif commit_point or hidden_parent_start >= 10000000: # if we are committing or have nothing hidden
                        #     earliest_possible_hidden = 100000000
                        
                        trie = trie.children[next_byte]

                # if there is more than one option we cannot advance without computing the logits 
                elif next_byte_mask_sum != 1:
                    break
            forced_pos = parser.pos # record how far the bytes are forced

            if retry_token_gen:
                continue

            # back up if we got forced up to a point that is not a valid token
            if next_byte_mask_sum <= 1:
                while trie.value is None and trie.parent is not None:
                    trie = trie.parent
                    forced_pos -= 1
                parser.pos = forced_pos
            
            # if we walked all the way to a forced token then we advance without computing the logits
            # we are forced if there are no more options and we are either in the middle of the grammar or at a trie leaf
            is_forced = next_byte_mask_sum <= 1 and (len(trie.children) == 0 if parser.matched() else trie != self._token_trie)
            if is_forced:
                sampled_token_ind = trie.value
                sampled_token = self.tokens[sampled_token_ind]
                new_bytes_log_prob = 0.0

            # we are at the end of the grammar
            elif next_byte_mask_sum == 0:
                token_pos = 0
                    
            # otherwise we need to compute the logits and sample a valid token
            else:
                logits = self._get_logits(token_ids)

                # if requested we compute the log probabilities so we can track the probabilities of each node
                # TODO: we should lower this step to C++ with pybind11
                if log_probs:
                    _compute_log_probs(trie, torch.nn.functional.log_softmax(logits, dim=-1).cpu().numpy())

                # get the sampling order
                if temperature == 0:
                    sampling_order = torch.argsort(logits, descending=True).cpu().numpy() # we need numpy so the enumerate below does not get really slow...
                else:
                    assert top_p == 1, "Still need to add support for top_p!"
                    probs = torch.nn.functional.softmax(logits / temperature, dim=-1)
                    sampling_order = torch.multinomial(probs, len(probs)).cpu().numpy()

                # loop over the tokens looking for a valid one
                for i,sampled_token_ind in enumerate(sampling_order):
                    sampled_token = self.tokens[sampled_token_ind]

                    # make sure the parse is backed up to the position we want to start checking from TODO: make this account for shared prefixes with the last token
                    parser.pos = forced_pos
                    new_bytes_log_prob = 0.0

                    # make sure it matches any forced prefix
                    if start_pos < forced_pos and not sampled_token.startswith(parser.bytes[start_pos:forced_pos]):
                        continue
                    offset = forced_pos - start_pos

                    # check to see if the sampled token is allowed
                    token_pos = offset
                    node = trie # this is the Trie node we were left at when we could force the next byte above

                    while token_pos < len(sampled_token):
                        next_byte = sampled_token[token_pos:token_pos+1]
                        next_node = node.children[next_byte]

                        # if we don't have a cached match flag compute it using the grammar
                        if next_node.match_version < self._token_trie.match_version:
                            next_byte_mask = parser.next_byte_mask()
                            for byte in node.children: # we update all the children since the parser knows the full mask
                                child = node.children[byte]
                                child.match_version = self._token_trie.match_version
                                child.match = next_byte_mask[byte[0]]
                        
                        # advance or fail according to the (now up-to-date) match cache
                        if next_node.match:
                            log_prob_delta = next_node.log_prob - node.log_prob
                            new_bytes_log_prob += log_prob_delta
                            parser.consume_byte(next_byte, log_prob=log_prob_delta)
                            # commit_node = parser.hidden_commit_point()
                            # if commit_node is not None:
                            #     to_force = b''
                            #     if generated_pos < commit_node.start:
                            #         to_force += parser.bytes[generated_pos:commit_node.start]
                            #     generated_pos = len(parser.bytes) # skip over the hidden bytes



                            #     # break out here and rewind the tokens to remove the data from the hidden commit point
                            #     # advance our generated_pos past the hidden commit_point node bytes (keeping whatever was before them as delayed_bytes)
                            #     # this means we need to set up some bytes to be "forced" without advancing the parser (those between generated_pos and the commit point node state)
                            node = next_node
                            token_pos += 1
                            if token_pos == len(sampled_token):
                                break # this token is valid
                        else:
                            # partially valid tokens are okay if we are running off the end of a grammar, but not otherwise
                            if not parser.matched():
                                token_pos = -1

                            break # this token is no longer valid

                    # check if this token is dominated by other longer valid tokens (and hence would never be consistent with greedy tokenization)
                    if token_pos == len(sampled_token) and not parser.matched(): # not we don't check if we have matched, because then we can generate anything afterwards
                        if _check_dominated(node, parser, self._token_trie.match_version, parser.next_byte_mask()):
                            token_pos = -1

                    if token_pos > 0:
                        break # we found a valid token

                    if parser.matched():
                        break # if we already have a full match we don't try more tokens we just give up as soon as the model deviates from the grammar

            # check for each position if we have closed a hidden node (and so need to prune it)
            # for pos in range(generated_pos, parser.pos):
            #     node = parser.closed_hidden_point(pos) # finds any hidden nodes that are completed at position pos
            #     if node is not None:
            #         node.start



            # emit whatever we know will not be hidden
            new_bytes = parser.bytes[generated_pos:parser.earliest_hidden_start()]

            # if we cannot consume any more tokens then we are done
            if not is_forced and token_pos < len(sampled_token) and trie == self._token_trie:
                assert parser.matched()

                # TODO: if we exactly match the end of the pattern then we can commit to this last token 
                # if m.span()[1] == len(generated_text):
                #     self._cache_state["new_token_ids"].append(sampled_token_ind)

                # capture the named groups from the parse tree
                parse_tree = parser.parse_tree()
                data = {}
                log_prob_data = {}
                _record_captures(parse_tree, data, log_prob_data, parser.bytes, 0)
                
                # we have no valid log prob data if we didn't compute it
                if not log_probs:
                    log_prob_data = {k: None for k in data}

                yield new_bytes[hidden_count:], not is_forced, new_bytes_log_prob, data, log_prob_data
                break # we are done!
            else:
                generated_pos += len(new_bytes)

                # yeild the snippet of text created by the next token
                out = new_bytes[hidden_count:]
                if len(out) > 0:
                    yield out, not is_forced, new_bytes_log_prob, {}, {} # note that we don't capture groups until a complete parse right now...
                    hidden_count = 0
                    token_count += 1 # note we only update this for tokens that emit non-hidden content
                else:
                    hidden_count -= len(new_bytes)

                token_ids.append(sampled_token_ind)

                # track the byte position of each token
                if len(token_byte_positions) == 0:
                    token_byte_positions.append(len(sampled_token))
                else:
                    token_byte_positions.append(token_byte_positions[-1] + len(sampled_token))

def _record_captures(item, data, log_prob_data, byte_data, byte_pos):
    
    # terminal nodes
    if isinstance(item, Terminal):

        # if we are at a capture group node then we save the matched terminal byte
        if item.capture_name is not None:
            data[item.capture_name] = item.byte
            log_prob_data[item.capture_name] = 0
    
    # internal nodes
    else:

        # if we are at a capture group node then we save the matched bytes range
        if item.node.capture_name is not None:
            data[item.node.capture_name] = byte_data[byte_pos:item.start] # note that "start" means "end" since this is a reversed state set
            log_prob_data[item.node.capture_name] = item.log_prob

        # recurse for all our non-null children
        for child in item.children:
            if child is not None:
                _record_captures(child, data, log_prob_data, byte_data, byte_pos)
                if isinstance(child, Terminal):
                    byte_pos += len(child)
                else:
                    byte_pos = child.start # note that "start" means "end" since this is a reversed state set
        

def _compute_log_probs(trie, log_probs):
    '''Computes the log probabilities for each internal trie node.'''
    if trie.value is not None:
        trie.log_prob += log_probs[trie.value]
    
    if len(trie.children) > 0:
        child_log_probs = []
        for b in trie.children:
            child = trie.children[b]
            _compute_log_probs(child, log_probs)
            child_log_probs.append(child.log_prob)
        trie.log_prob = np.logaddexp.reduce(child_log_probs)

def _check_dominated(node, parser, match_version, next_byte_mask):
    curr_pos = parser.pos
    for byte_num in next_byte_mask.nonzero()[0]:
        next_byte = bytes((byte_num,))
        if next_byte not in node.children:
            return False # no possible exension this direction, so we are not dominated
        child = node.children[next_byte]
        if child.match_version < match_version:
            child.match_version = match_version
            child.match = next_byte_mask[next_byte[0]]
        
        if not child.match:
            return False # this child does not dominate the node, so the node is not dominated
        elif child.value is None: # this child might not dominate the node
            parser.consume_byte(next_byte, log_prob=0.0)
            child_dominate = _check_dominated(child, parser, match_version, parser.next_byte_mask())
            parser.pos = curr_pos
            if not child_dominate:
                return False
    return True
            

# this token can only be dominated if we are at the end of this token

# dominated = True # assume true until proven otherwise
# check_stack = [(node, parser.pos)]
# for byte_num in next_byte_mask.nonzero()[0]:
#     node, pos = check_stack.pop()
#     if node.match_version < self._token_trie.match_version:
#         parser.pos
#         next_byte_mask = parser.next_byte_mask()
#         for byte in node.children: # we update all the children since the parser knows the full mask
#             child = node.children[byte]
#             child.match_version = self._token_trie.match_version
#             child.match = next_byte_mask[byte[0]]
#     byte = bytes((byte_num,))
#     child = node.children[byte]
#     if not child.match:
#         dominated = False
#         break
#     else:
#         # if this is not a valid token yet we need to determine if this child is dominated
#         if child.value is None:
#             check_stack.push((child, pos))

# we invalidate this token if it is dominated
# if dominated:
#     token_pos = -1