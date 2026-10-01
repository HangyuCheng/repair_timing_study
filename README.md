# An Empirical Study of Repair Timing in Automated Program Repair

## 👋 Introduction
This is the repository of the paper _An Empirical Study of Repair Timing in Automated Program Repair_.

## 📄 Project Structure
```text
├─📁 JITBench_Construction
│ ├─📁 bug_reconstruction
│ ├─📁 collect
│ └─📁 validation
├─📁 config_
├─📁 evaluation
├─📁 scripts
├─📁 utils
├─📄 README.md
└─📄 __main__.py
```
> **JITBench_Construction** includes the pipeline used to construct our benchmark.<br>
> **config_** includes experiment settings, model endpoints, and vLLM deployment parameters.<br>
> **evaluation** includes benchmark loading, prompt construction, model invocation, and patch generation.<br>
> **scripts** includes project cloning utilities and scripts for setting up vLLM and launching model services.<br>
> **utils** includes shared command line interfaces, data I/O functions, and schema consistency validation.<br>
## 🛠️ Set Up

Run the following commands from the project root to initialize the Python environment and install JITBench:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e
```

Create configuration files:

```bash
cp config/project_collection.example.yaml config/project_collection.yaml
cp config/projects.example.yaml config/projects.yaml
cp config/validation.example.yaml config/validation.yaml
```

Configure the API keys required by your selected models and GitHub project collection:

```bash
export OPENAI_API_KEY="your-openai-api-key"
export GEMINI_API_KEY="your-gemini-api-key"
export DEEPSEEK_API_KEY="your-deepseek-api-key"
export GITHUB_TOKEN="your-github-token"
```

Initialize the independent vLLM environment:

```bash
bash scripts/setup_vllm.sh
```
> **Note:** Replace the placeholder API keys with your own credentials. GumTree, Joern, the required JDK versions, and project build tools must be installed separately.

### To run the JITbench construct pipeline


```bash
cd JITBench_Construction
ROOT=../../..

python -m jitbench construction crawl-projects --config $ROOT/config/project_collection.yaml --out $ROOT/work/projects.jsonl
python -m jitbench construction select-projects --config $ROOT/config/project_collection.yaml --candidates $ROOT/work/projects.jsonl --clone-root $ROOT/work/repositories --out $ROOT/config/projects.yaml
python -m jitbench construction prepare --config $ROOT/config/projects.yaml --out $ROOT/work/candidates.jsonl
python -m jitbench construction evidence --projects $ROOT/config/projects.yaml --candidates $ROOT/work/candidates.jsonl --out $ROOT/work/evidence
python -m jitbench construction validate-decisions --decisions $ROOT/work/evidence/decisions.jsonl
python -m jitbench construction reconstruct --projects $ROOT/config/projects.yaml --decisions $ROOT/work/evidence/decisions.jsonl --out $ROOT/work/JITBench.reconstructed.json
python -m jitbench construction metadata --projects $ROOT/config/projects.yaml --benchmark $ROOT/work/JITBench.reconstructed.json --out $ROOT/work/JITBench.with-metadata.json
python -m jitbench validation run --projects $ROOT/config/projects.yaml --benchmark $ROOT/work/JITBench.with-metadata.json --manifest $ROOT/config/validation.yaml --work-root $ROOT/work/validation --out $ROOT/work/validation-results.json
python -m jitbench construction finalize --benchmark $ROOT/work/JITBench.with-metadata.json --validation $ROOT/work/validation-results.json --out $ROOT/outputs/JITBench.json
```

 
Extract the data:

```bash
cd JITBench_Construction
tar -xjf JITBench_clean_subset.tar.bz2
```
> [!NOTE]
> Only part of the dataset (specifically, our clean subset) is avaliable for now. We will release the full dataset upon acceptance of this paper.

## 🚀 Evaluation

Enter the evaluation directory:

```bash
cd evaluation
```

Run the evaluation:

```bash
python -m evaluation run \
  --benchmark ../JITBench_Construction/JITBench_clean_subset \
  --generations ./res_artifact.json \
  --validation ../../../config_/validation.yaml \
  --projects ../../../config_/projects.yaml \
  --out ../../../outputs/evaluation \
  --ast-engine gumtree
```

