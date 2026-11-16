// Runs the TinyStories model directly on the Raspberry Pi.
// Jobs arrive line by line on stdin, the answer goes to stdout:
//   GEN <temperature> <top_k> <count> <id> <id> ...
//
//   cc -O3 -mcpu=cortex-a53 -o tinystories_pi tinystories_pi.c -lm
//   ./tinystories_pi model.bin
//
// llm.h and vocab.h come from the esp32-ai project, see Licence.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <time.h>

#define LLM_INT8_ACT 1
#include "llm.h"
#include "vocab.h"

#define MAX_PROMPT 128
#define MAX_TOP_K 100
#define MAX_GENERATE 256
#define EOS_TOKEN 0           // <|endoftext|>
#define END_OF_TEXT '\x1e'    // separates the text from the statistics

static Model model;
static Scratch s;

static void *alloc_or_die(size_t n) {
  void *p = malloc(n);
  if (!p) {
    printf("FATAL: out of memory allocating %zu bytes\n", n);
    exit(1);
  }
  return p;
}

static uint8_t *read_file(const char *path) {
  FILE *f = fopen(path, "rb");
  if (!f) {
    printf("FATAL: cannot open %s\n", path);
    exit(1);
  }
  fseek(f, 0, SEEK_END);
  size_t size = ftell(f);
  fseek(f, 0, SEEK_SET);
  uint8_t *data = alloc_or_die(size);
  if (fread(data, 1, size, f) != size) {
    printf("FATAL: %s is incomplete\n", path);
    exit(1);
  }
  fclose(f);
  return data;
}

static void alloc_scratch(void) {
  Cfg *c = &model.c;
  int D = c->dim, L = c->n_layers, P = c->ple_dim, F = c->ffn, S = c->seq_len;
  s.x = alloc_or_die(D * 4);
  s.h = alloc_or_die((F > D ? F : D) * 4);
  s.qkv = alloc_or_die(3 * D * 4);
  s.att = alloc_or_die(D * 4);
  s.g1 = alloc_or_die(F * 4);
  s.g2 = alloc_or_die((P > F ? P : F) * 4);
  s.ple = alloc_or_die(L * P * 4);
  s.tmpP = alloc_or_die(L * P * 4);
  s.trow = alloc_or_die(L * P * 4);
  s.scores = alloc_or_die(S * 4);
  s.logits = alloc_or_die((size_t)model.out_vocab * 4);
  s.kcache = alloc_or_die((size_t)L * S * D * 4);
  s.vcache = alloc_or_die((size_t)L * S * D * 4);
}

static void load_model(const char *path) {
  if (llm_load(read_file(path), &model)) {
    printf("FATAL: %s is not a valid model\n", path);
    exit(1);
  }
  if (VOCAB_N != model.out_vocab) {
    printf("FATAL: vocab.h has %d entries, the model has %d\n", VOCAB_N, model.out_vocab);
    exit(1);
  }
  alloc_scratch();

  int want = llm_core_stage_count(&model);
  int staged = llm_stage_core_int8_alloc(&model, malloc);
  if (staged != want) {
    printf("FATAL: prepared only %d of %d tensors\n", staged, want);
    exit(1);
  }
  llm_stage_int8(&model.out_head, alloc_or_die(llm_stage_int8_bytes(&model.out_head)));
}

static double now_s(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

static float random_unit(void) {
  uint32_t value = 0;
  if (getrandom(&value, sizeof value, 0) != sizeof value) {
    printf("FATAL: no random numbers available\n");
    exit(1);
  }
  return value / 4294967296.0f;
}

static void emit(int tok) {
  if (tok >= VOCAB_N) return;
  fwrite(VOCAB_BLOB + VOCAB_OFF[tok], 1, VOCAB_OFF[tok + 1] - VOCAB_OFF[tok], stdout);
  fflush(stdout);
}

// Picks the next token: temperature 0 takes the most likely one, otherwise it is
// drawn from the top_k best candidates.
static int pick_token(float temperature, int top_k) {
  int best_id[MAX_TOP_K];
  float best_val[MAX_TOP_K];
  int count = 0;
  const int keep = temperature > 0.f ? top_k : 1;

  for (int v = 0; v < model.out_vocab; v++) {
    float val = s.logits[v];
    if (count == keep && val <= best_val[count - 1]) continue;
    int i = count < keep ? count++ : keep - 1;
    while (i > 0 && best_val[i - 1] < val) {
      best_val[i] = best_val[i - 1];
      best_id[i] = best_id[i - 1];
      i--;
    }
    best_val[i] = val;
    best_id[i] = v;
  }
  if (temperature <= 0.f) return best_id[0];

  float weights[MAX_TOP_K];
  float total = 0.f;
  for (int i = 0; i < count; i++) {
    weights[i] = expf((best_val[i] - best_val[0]) / temperature);
    total += weights[i];
  }
  float r = random_unit() * total;
  for (int i = 0; i < count; i++) {
    r -= weights[i];
    if (r < 0.f) return best_id[i];
  }
  return best_id[count - 1];
}

// Ends a job: separator, then "<count> <tokens per second>".
static void finish_reply(int generated, double tokens_per_s) {
  printf("%c%d %.2f\n", END_OF_TEXT, generated, tokens_per_s);
  fflush(stdout);
}

static void fail(const char *message) {
  printf("ERR %s\n", message);
  finish_reply(0, 0.0);
}

static void generate(const int *prompt, int n_prompt, float temperature, int top_k,
                     int n_generate) {
  int pos = 0;
  for (int i = 0; i < n_prompt; i++) {
    llm_forward(&model, prompt[i], pos++, &s);
  }

  int generated = 0;
  double start = now_s();
  while (generated < n_generate && pos < model.c.seq_len) {
    int tok = pick_token(temperature, top_k);
    if (tok == EOS_TOKEN) break;
    emit(tok);
    llm_forward(&model, tok, pos++, &s);
    generated++;
  }
  double elapsed = now_s() - start;
  finish_reply(generated, generated > 0 ? generated / elapsed : 0.0);
}

static void handle_command(char *line) {
  static int prompt[MAX_PROMPT];

  if (strncmp(line, "GEN ", 4) != 0) { fail("unknown command"); return; }

  char *end;
  float temperature = strtof(line + 4, &end);
  long top_k = strtol(end, &end, 10);
  long n_generate = strtol(end, &end, 10);

  int n_prompt = 0;
  for (;;) {
    char *after;
    long id = strtol(end, &after, 10);
    if (after == end) break;
    if (id < 0 || id >= model.c.vocab) { fail("token id out of range"); return; }
    if (n_prompt == MAX_PROMPT) { fail("the start is too long"); return; }
    prompt[n_prompt++] = id;
    end = after;
  }

  if (!(temperature >= 0.f && temperature <= 2.f)) { fail("temperature must be between 0 and 2"); return; }
  if (top_k < 1 || top_k > MAX_TOP_K) { fail("top_k must be between 1 and 100"); return; }
  if (n_generate < 1 || n_generate > MAX_GENERATE) { fail("count must be between 1 and 256"); return; }
  if (n_prompt == 0) { fail("the start is missing"); return; }

  generate(prompt, n_prompt, temperature, top_k, n_generate);
}

int main(int argc, char **argv) {
  if (argc != 2) {
    fprintf(stderr, "usage: %s <model.bin>\n", argv[0]);
    return 2;
  }
  load_model(argv[1]);
  puts("READY");
  fflush(stdout);

  static char line[1024];
  while (fgets(line, sizeof line, stdin)) {
    line[strcspn(line, "\r\n")] = '\0';
    if (line[0] != '\0') handle_command(line);
  }
  return 0;
}
