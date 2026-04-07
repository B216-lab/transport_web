pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                echo 'Сборка проекта...'
                // Ваши команды для Linux, например: sh 'make'
            }
        }
        stage('Test') {
            steps {
                echo 'Тестирование...'
                // Ваши команды для Linux, например: sh 'make test'
            }
        }
        stage('Deploy') {
            steps {
                echo 'Развертывание...'
                // Ваши команды для Linux, например: sh './deploy.sh'
            }
        }
    }
}
