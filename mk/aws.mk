# AWS deployment of Cairn (ap-south-1): Neon is the database, everything else is AWS.
# See docs/deploy-aws.md. Targets here are user-run (they use your AWS credentials and .env.neon)
# and are in no gate.

.PHONY: aws-secrets aws-package aws-cfn-check

aws-secrets: ## AWS, user-run: copy the Neon runtime settings from .env.neon into SSM (names only are printed)
	$(UV_RUN) python scripts/aws_push_neon_secrets.py $(if $(AWS_PROFILE),--profile $(AWS_PROFILE),)

aws-package: ## Pack the source for the EC2 build to var/cairn-src.tar.gz (refuses a tarball with secrets)
	@mkdir -p var
	deploy/aws/package.sh var/cairn-src.tar.gz

aws-cfn-check: ## Parse infra/aws/cairn.yaml (CloudFormation intrinsic tags allowed)
	$(UV_RUN) python -c "import yaml,sys; \
	yaml.add_multi_constructor('!', lambda l,s,n: None); \
	yaml.add_multi_constructor('', lambda l,s,n: None); \
	d=yaml.load(open('infra/aws/cairn.yaml'),Loader=yaml.Loader); print(sorted(d['Resources']))"
