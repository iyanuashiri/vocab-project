FROM public.ecr.aws/lambda/python:3.12


RUN pip install --upgrade pip
COPY requirements.txt .
RUN pip install -r requirements.txt --target "${LAMBDA_TASK_ROOT}"

COPY vocabularies.py ${LAMBDA_TASK_ROOT}


CMD ["vocabularies.lambda_handler"]
