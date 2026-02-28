# Machine Learning Basics

## Overview

Machine learning is a subset of artificial intelligence that enables systems to learn patterns from data without being explicitly programmed. It has become foundational to modern software, powering everything from recommendation engines and natural language processing to knowledge extraction and memory systems.

## Supervised Learning

In supervised learning, models are trained on labeled data where each input has a corresponding target output. The model learns a mapping function from inputs to outputs by minimizing a loss function.

Common supervised learning tasks include:

- **Classification**: Predicting discrete categories (e.g., spam detection, entity type classification).
- **Regression**: Predicting continuous values (e.g., house prices, relevance scores).

Popular algorithms include linear regression, logistic regression, decision trees, random forests, and support vector machines (SVMs). The choice of algorithm depends on the data characteristics, the problem type, and the trade-off between interpretability and accuracy.

## Unsupervised Learning

Unsupervised learning finds patterns in data without labeled examples. Key techniques include:

- **Clustering**: Grouping similar data points together. K-means and DBSCAN are widely used for tasks like customer segmentation and entity resolution, where duplicate or related entities need to be merged into canonical forms.
- **Dimensionality reduction**: Reducing the number of features while preserving meaningful structure. PCA (Principal Component Analysis) and t-SNE are used for visualization, while autoencoders learn compressed representations.
- **Anomaly detection**: Identifying unusual patterns that deviate from expected behavior, useful for fraud detection and system monitoring.

## Neural Networks and Deep Learning

Neural networks are composed of layers of interconnected nodes (neurons) that transform inputs through learned weights and non-linear activation functions.

### Architecture

A typical neural network consists of:

- **Input layer**: Receives raw features (text embeddings, image pixels, numerical features).
- **Hidden layers**: Apply learned transformations. More layers enable the network to learn increasingly abstract representations.
- **Output layer**: Produces predictions (class probabilities, regression values, generated text).

### Gradient Descent

Neural networks learn through gradient descent, an optimization algorithm that iteratively adjusts weights to minimize the loss function:

1. **Forward pass**: Input data flows through the network to produce a prediction.
2. **Loss computation**: The prediction is compared to the target using a loss function (e.g., cross-entropy for classification, MSE for regression).
3. **Backward pass (backpropagation)**: Gradients of the loss with respect to each weight are computed using the chain rule.
4. **Weight update**: Weights are adjusted in the direction that reduces the loss, scaled by a learning rate.

Variants like Adam, SGD with momentum, and AdaGrad adapt the learning rate during training for faster convergence.

## Embeddings and Vector Representations

Embeddings are dense vector representations that capture semantic meaning in a continuous space. They are central to modern NLP and information retrieval:

- **Word embeddings** (Word2Vec, GloVe): Map words to vectors where semantically similar words are close together.
- **Sentence embeddings** (Sentence-BERT, OpenAI embeddings): Encode entire sentences or paragraphs into fixed-size vectors.
- **Document embeddings**: Represent full documents for similarity search and clustering.

Vector similarity is typically measured using cosine similarity or Euclidean distance. Efficient nearest-neighbor search over millions of vectors requires specialized index structures like HNSW (Hierarchical Navigable Small World) graphs, which are implemented in databases like PostgreSQL with the pgvector extension.

## Retrieval and Ranking

Modern information retrieval combines traditional keyword search with semantic vector search:

- **BM25**: A probabilistic keyword-based ranking function that accounts for term frequency and document length.
- **Semantic search**: Uses embedding similarity to find conceptually related results even without exact keyword matches.
- **Reciprocal Rank Fusion (RRF)**: Combines rankings from multiple retrieval strategies into a single unified ranking, giving robust results without requiring score calibration.

These techniques are used together in hybrid retrieval systems where multiple strategies (semantic, keyword, temporal, entity-based) each contribute candidates, and fusion produces the final ranked result.

## Practical Applications

Machine learning enables knowledge management systems to automatically extract facts from documents, resolve entities across sources, generate embeddings for semantic search, and synthesize observations into mental models through reflection. Python is the dominant language for ML development, with libraries like PyTorch, scikit-learn, and Hugging Face Transformers providing the building blocks.
