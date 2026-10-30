Supported images run Ubuntu, except one that runs CentOS. Name system packages as the
image's distribution names them: build-essential on Ubuntu is gcc, gcc-c++ and make on
CentOS.

For work on an NVIDIA GPU, boot the image with CUDA in its suffix. It carries the driver
and CUDA. Ask for neither as a package. PyTorch and TensorFlow from pip bring their own
CUDA libraries.

Ask for Docker with GPU access when a container needs the GPU. A container runs its
image's own command. Choose an image that starts what the request asks for, such as a
Jupyter image for a notebook.
